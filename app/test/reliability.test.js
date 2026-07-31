// Reliability tests for the durable pieces: the settings store and the ingest
// queue. Plain Node — no Electron needed.
//
//   node app/test/reliability.test.js

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");

const { Store } = require("../store");
const { IngestQueue } = require("../queue");

let pass = 0;
let fail = 0;
const t = async (name, fn) => {
  try {
    await fn();
    pass++;
    console.log(`  PASS  ${name}`);
  } catch (e) {
    fail++;
    console.log(`  FAIL  ${name}\n        ${(e && e.message) || e}`);
  }
};
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "syt-test-"));

(async () => {
  console.log("\nStore — survives what a power cut does to a file");

  await t("round-trips accounts and prefs", () => {
    const d = tmp();
    const s = new Store(d);
    s.patchAccount("instagram", { connected: true, schedule: "daily" });
    s.setPref("launchAtLogin", true);
    s.flush();
    const s2 = new Store(d);
    assert.equal(s2.account("instagram").connected, true);
    assert.equal(s2.account("instagram").schedule, "daily");
    assert.equal(s2.prefs().launchAtLogin, true);
  });

  await t("a truncated settings file falls back to the backup, losing nothing", () => {
    const d = tmp();
    const s = new Store(d);
    s.patchAccount("facebook", { connected: true, schedule: "weekly" });
    s.flush();
    s.patchAccount("facebook", { schedule: "daily" }); // creates .bak on flush
    s.flush();
    // Simulate a power cut mid-write: the live file is half-written JSON.
    fs.writeFileSync(path.join(d, "app-settings.json"), '{"accounts":{"faceb');
    const s2 = new Store(d);
    assert.equal(s2.account("facebook").connected, true, "recovered connection");
    assert.ok(fs.readdirSync(d).some((f) => f.includes("corrupt")), "kept the corrupt file");
  });

  await t("a totally missing file yields safe defaults, not a crash", () => {
    const s = new Store(tmp());
    assert.deepEqual(s.account("nope"), { schedule: "manual", connected: false });
    assert.equal(typeof s.prefs(), "object");
  });

  await t("writes are atomic — no partial file is ever visible", () => {
    const d = tmp();
    const s = new Store(d);
    for (let i = 0; i < 40; i++) {
      s.patchAccount("x", { n: i });
      s.flush();
      const raw = fs.readFileSync(path.join(d, "app-settings.json"), "utf8");
      JSON.parse(raw); // must ALWAYS parse; rename is atomic
    }
    assert.equal(s.account("x").n, 39);
  });

  console.log("\nIngestQueue — never drops a user's export");

  await t("two exports arriving at once are BOTH ingested (the old bug)", async () => {
    const d = tmp();
    const store = new Store(d);
    const a = path.join(d, "a.zip");
    const b = path.join(d, "b.zip");
    fs.writeFileSync(a, "a");
    fs.writeFileSync(b, "b");
    const seen = [];
    let concurrent = 0;
    let maxConcurrent = 0;
    const q = new IngestQueue(
      store,
      async (p) => {
        maxConcurrent = Math.max(maxConcurrent, ++concurrent);
        await new Promise((r) => setTimeout(r, 40));
        concurrent--;
        seen.push(path.basename(p));
        return { ok: true, stdout: "Backed up 3 items" };
      },
      () => {}
    );
    const [r1, r2] = await Promise.all([q.add(a), q.add(b)]);
    assert.equal(r1, true, "first ingested");
    assert.equal(r2, true, "second ingested — not silently dropped");
    assert.deepEqual(seen.sort(), ["a.zip", "b.zip"]);
    assert.equal(maxConcurrent, 1, "ingests are serialized, never concurrent");
  });

  await t("a failing ingest is retried, then given up on without deleting the file", async () => {
    const d = tmp();
    const store = new Store(d);
    const f = path.join(d, "bad.zip");
    fs.writeFileSync(f, "x");
    let calls = 0;
    const events = [];
    const q = new IngestQueue(
      store,
      async () => {
        calls++;
        return { ok: false, stderr: "boom" };
      },
      (e) => events.push(e.phase)
    );
    const ok = await q.add(f, { cleanup: true });
    assert.equal(ok, false);
    assert.ok(calls > 1, `retried (was called ${calls}x)`);
    assert.ok(fs.existsSync(f), "the file is KEPT so the user can retry");
    assert.ok(events.includes("error"), "reported a final error");
  });

  await t("an unreadable export fails once, with the reason, and is NOT retried", async () => {
    // Found in real testing: an Instagram HTML export (JSON was needed). The
    // engine can never read it, so retrying four times per launch is waste —
    // and it buries the one sentence that tells the user what to do.
    const d = tmp();
    const store = new Store(d);
    const f = path.join(d, "instagram-html-export.zip");
    fs.writeFileSync(f, "x");
    let calls = 0;
    const events = [];
    const q = new IngestQueue(
      store,
      async () => {
        calls++;
        return {
          ok: false,
          stdout: "",
          stderr:
            "✗ Could not read this export: instagram-html-export.zip\n" +
            "  • This export was not recognized by any connector.\n" +
            "  • It looks like an HTML export — re-download choosing JSON format, " +
            "which is what this tool reads.\n" +
            "SYT_UNRECOGNIZED_EXPORT: could not recognize this export.",
        };
      },
      (e) => events.push(e)
    );
    const ok = await q.add(f, { cleanup: false });
    assert.equal(ok, false);
    assert.equal(calls, 1, `tried exactly once (was ${calls})`);
    const err = events.find((e) => e.phase === "error");
    assert.ok(err && err.permanent === true, "reported as a permanent failure");
    assert.match(err.error, /re-download choosing JSON/, "surfaces the actionable sentence");
    assert.ok(fs.existsSync(f), "the user's file is left alone");
    assert.equal(store.load().queue.length, 0, "removed from the queue");
  });

  await t("a transient failure is still retried", async () => {
    const d = tmp();
    const store = new Store(d);
    const f = path.join(d, "ok-later.zip");
    fs.writeFileSync(f, "x");
    let calls = 0;
    const q = new IngestQueue(
      store,
      async () => {
        calls++;
        return calls < 3
          ? { ok: false, stderr: "database is locked" }
          : { ok: true, stdout: "Backed up 9 items" };
      },
      () => {}
    );
    assert.equal(await q.add(f), true, "eventually succeeded");
    assert.equal(calls, 3, "retried until it worked");
  });

  await t("a successful ingest deletes only files we created", async () => {
    const d = tmp();
    const store = new Store(d);
    const ours = path.join(d, "ours.zip");
    const theirs = path.join(d, "theirs.zip");
    fs.writeFileSync(ours, "1");
    fs.writeFileSync(theirs, "2");
    const q = new IngestQueue(store, async () => ({ ok: true, stdout: "Backed up" }), () => {});
    await q.add(ours, { cleanup: true });
    await q.add(theirs, { cleanup: false });
    assert.equal(fs.existsSync(ours), false, "our temp download is cleaned up");
    assert.equal(fs.existsSync(theirs), true, "the user's own file is never deleted");
  });

  await t("a crash mid-queue is recovered on next launch", async () => {
    const d = tmp();
    const inc = path.join(d, "incoming");
    fs.mkdirSync(inc);
    const f = path.join(inc, "instagram-123-export.zip");
    fs.writeFileSync(f, "data");
    // Simulate: app died after download, before/while ingesting.
    const store = new Store(d);
    store.load().queue = [{ path: f, attempts: 1, cleanup: true }];
    store.flush();

    const store2 = new Store(d);
    const done = [];
    const q = new IngestQueue(
      store2,
      async (p) => {
        done.push(p);
        return { ok: true, stdout: "Backed up" };
      },
      () => {}
    );
    q.recover(inc);
    await new Promise((r) => setTimeout(r, 120));
    assert.deepEqual(done, [f], "the interrupted export was picked up and backed up");
  });

  await t("orphans in incoming/ (never queued) are also recovered", async () => {
    const d = tmp();
    const inc = path.join(d, "incoming");
    fs.mkdirSync(inc);
    const orphan = path.join(inc, "twitter-999-archive.zip");
    fs.writeFileSync(orphan, "data");
    const store = new Store(d);
    const done = [];
    const q = new IngestQueue(
      store,
      async (p) => {
        done.push(p);
        return { ok: true, stdout: "Backed up" };
      },
      () => {}
    );
    const n = q.recover(inc);
    await new Promise((r) => setTimeout(r, 120));
    assert.equal(n, 1);
    assert.deepEqual(done, [orphan]);
  });

  await t("queue entries whose file vanished are dropped, not retried forever", async () => {
    const d = tmp();
    const store = new Store(d);
    store.load().queue = [{ path: path.join(d, "gone.zip"), attempts: 0 }];
    store.flush();
    const q = new IngestQueue(store, async () => ({ ok: true }), () => {});
    q.recover(path.join(d, "nope"));
    await new Promise((r) => setTimeout(r, 60));
    assert.equal(store.load().queue.length, 0);
  });

  await t("the queue is durable on disk BEFORE work starts", async () => {
    const d = tmp();
    const store = new Store(d);
    const f = path.join(d, "x.zip");
    fs.writeFileSync(f, "x");
    let sawOnDisk = false;
    const q = new IngestQueue(
      store,
      async () => {
        // While ingesting, a *fresh* reader must already see the entry — that
        // is what makes crash recovery possible.
        const fresh = new Store(d);
        sawOnDisk = fresh.load().queue.some((e) => e.path === f);
        return { ok: true, stdout: "Backed up" };
      },
      () => {}
    );
    await q.add(f);
    assert.ok(sawOnDisk, "entry was persisted before the ingest ran");
  });

  console.log(`\n${pass}/${pass + fail} checks passed`);
  process.exit(fail ? 1 : 0);
})();
