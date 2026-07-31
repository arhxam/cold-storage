// queue.js — a durable, serialized ingest queue.
//
// Two exports can finish downloading at the same moment, and `syt ingest` must
// not run twice at once against one archive. The old code guarded that with a
// boolean and *dropped* the second file — silent data loss, which is the exact
// failure this product exists to prevent.
//
// So: every file to ingest is appended to a queue that is persisted before the
// work starts. One worker drains it. If the app crashes or the machine loses
// power mid-ingest, the entry is still on disk and is retried on next launch.
// Entries are only removed once the engine reports success (or the file is
// gone, or it has failed too many times to be worth retrying).

const fs = require("fs");

const MAX_ATTEMPTS = 4;
// Big archives are slow; this is a backstop against a wedged child, not a
// perf budget. Instagram/Google exports can legitimately take many minutes.
const INGEST_TIMEOUT_MS = 90 * 60 * 1000;

class IngestQueue {
  /**
   * @param store  the Store (queue is persisted inside it)
   * @param runIngest  async (path) => {ok, stdout, stderr}
   * @param onEvent  (event) => void  — for UI toasts / status
   */
  constructor(store, runIngest, onEvent) {
    this.store = store;
    this.runIngest = runIngest;
    this.onEvent = onEvent || (() => {});
    this.draining = false;
    this.current = null;
  }

  get items() {
    return this.store.load().queue;
  }

  /** Add a file. Returns a promise resolving to true if it eventually ingested. */
  add(filePath, meta = {}) {
    if (!filePath) return Promise.resolve(false);
    const q = this.items;
    if (q.some((e) => e.path === filePath)) {
      // Already queued — attach to the existing entry rather than duplicating.
      return new Promise((resolve) => this._waiters(filePath).push(resolve));
    }
    q.push({
      path: filePath,
      platform: meta.platform || null,
      addedAt: new Date().toISOString(),
      attempts: 0,
      // Files we copied into incoming/ are ours to delete; a file the user
      // picked from Downloads is not.
      cleanup: !!meta.cleanup,
    });
    this.store.save();
    this.store.flush(); // durable before we start: a crash must not lose it
    const p = new Promise((resolve) => this._waiters(filePath).push(resolve));
    this.drain();
    return p;
  }

  _waiters(filePath) {
    this._w = this._w || new Map();
    if (!this._w.has(filePath)) this._w.set(filePath, []);
    return this._w.get(filePath);
  }

  _settle(filePath, ok) {
    if (!this._w || !this._w.has(filePath)) return;
    for (const r of this._w.get(filePath)) {
      try {
        r(ok);
      } catch {
        /* ignore */
      }
    }
    this._w.delete(filePath);
  }

  /** Re-queue anything left over from a previous run (crash recovery). */
  recover(incomingDir) {
    const q = this.items;
    // Drop entries whose file vanished.
    const alive = q.filter((e) => {
      if (fs.existsSync(e.path)) return true;
      this._settle(e.path, false);
      return false;
    });
    if (alive.length !== q.length) {
      this.store.load().queue = alive;
      this.store.save();
    }
    // Pick up orphans: files that were downloaded but never made it onto the
    // queue (killed between save and add).
    try {
      for (const name of fs.readdirSync(incomingDir)) {
        const full = require("path").join(incomingDir, name);
        if (alive.some((e) => e.path === full)) continue;
        try {
          if (!fs.statSync(full).isFile()) continue;
        } catch {
          continue;
        }
        alive.push({
          path: full,
          platform: (name.split("-")[0] || null),
          addedAt: new Date().toISOString(),
          attempts: 0,
          cleanup: true,
        });
      }
      this.store.load().queue = alive;
      this.store.save();
    } catch {
      /* incoming/ may not exist yet */
    }
    if (alive.length) this.drain();
    return alive.length;
  }

  async drain() {
    if (this.draining) return;
    this.draining = true;
    try {
      for (;;) {
        const q = this.items;
        const entry = q[0];
        if (!entry) break;

        if (!fs.existsSync(entry.path)) {
          q.shift();
          this.store.save();
          this._settle(entry.path, false);
          continue;
        }

        this.current = entry.path;
        entry.attempts = (entry.attempts || 0) + 1;
        this.store.save();
        this.onEvent({ phase: "start", path: entry.path, platform: entry.platform });

        let res;
        try {
          res = await this._withTimeout(this.runIngest(entry.path), INGEST_TIMEOUT_MS);
        } catch (e) {
          res = { ok: false, stdout: "", stderr: String((e && e.message) || e) };
        }
        this.current = null;

        if (res && res.ok) {
          q.shift();
          this.store.save();
          this.store.flush();
          if (entry.cleanup) {
            try {
              fs.rmSync(entry.path, { force: true });
            } catch {
              /* leave it; it will be skipped next time as already-ingested */
            }
          }
          const summary =
            (String(res.stdout).match(/Backed up .*$/m) || [
              String(res.stdout).trim() || "Backup complete.",
            ])[0];
          this.onEvent({ phase: "done", path: entry.path, platform: entry.platform, summary });
          this._settle(entry.path, true);
          continue;
        }

        const detail =
          (String((res && res.stderr) || (res && res.stdout) || "").trim().slice(-500)) ||
          "unknown error";
        if (entry.attempts >= MAX_ATTEMPTS) {
          q.shift();
          this.store.save();
          this.store.flush();
          // Deliberately do NOT delete the file: the user can retry by hand,
          // and a broken parse is worth keeping the evidence for.
          this.onEvent({
            phase: "error",
            path: entry.path,
            platform: entry.platform,
            error: detail,
            final: true,
          });
          this._settle(entry.path, false);
          continue;
        }
        // Transient: leave it at the head and back off before retrying.
        this.onEvent({
          phase: "retry",
          path: entry.path,
          platform: entry.platform,
          error: detail,
          attempts: entry.attempts,
        });
        this.store.save();
        await new Promise((r) => setTimeout(r, 5000 * entry.attempts));
      }
    } finally {
      this.draining = false;
      this.current = null;
    }
  }

  _withTimeout(promise, ms) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("ingest timed out")), ms);
      promise.then(
        (v) => {
          clearTimeout(t);
          resolve(v);
        },
        (e) => {
          clearTimeout(t);
          reject(e);
        }
      );
    });
  }
}

module.exports = { IngestQueue, MAX_ATTEMPTS };
