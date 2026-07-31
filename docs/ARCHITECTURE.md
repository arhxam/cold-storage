# How it all connects

Save Your Shit is one small engine with several faces. Everything runs on your
machine; the only network traffic is to the platforms (to fetch your exports) and,
optionally, to *your own* cloud storage. There is no server we operate.

## The one-glance picture

```
  You download an export          ┌──────────────────────────────────────────┐
  from a platform  ───────────►   │  a folder like ~/Downloads                 │
  (Instagram, X, Telegram, …)     │   ├─ instagram.zip                         │
                                  │   ├─ twitter-archive/                      │
                                  │   └─ telegram-result.json                  │
                                  └───────────────┬────────────────────────────┘
                                                  │  syt ingest <path>
                                                  │  syt ingest ~/Downloads --all   ← simplest trigger
                                                  ▼
   ┌───────────────────────────── ENGINE (engine.py) ─────────────────────────────┐
   │  1. detect which platform  →  connectors/*.py  (one per platform, Rail A)     │
   │  2. snapshot the raw export, untouched, under <platform>/snapshots/<date>/    │
   │  3. parse → normalize into canonical records (models.py) + media refs         │
   │  4. hand each Batch to the Archive                                            │
   └───────────────────────────────────┬──────────────────────────────────────────┘
                                        ▼
   ┌───────────────────────────── ARCHIVE (store/) ───────────────────────────────┐
   │  • blobs/      content-addressed, deduped, AES-256 encrypted media           │
   │  • index.sqlite  SQLite + FTS5 — search + conversations (threads)            │
   │  • <platform>/manifest.jsonl  append-only, diffable log of everything        │
   │  • keys/       your master key, wrapped by your passphrase (crypto/)         │
   └───────────────┬───────────────────────────────────────┬──────────────────────┘
                   │                                        │
        reads (decrypted, local)                   optional, encrypted
                   │                                        ▼
   ┌───────────────┴─────────────┐            ┌─────────────────────────────────┐
   │  FACES                       │            │  OFFSITE (sync/)                │
   │  • CLI      syt status/search│            │  restic → versioned encrypted   │
   │  • Web app  syt serve  (chat)│            │  rclone → your B2/Drive/Dropbox │
   │  • Viewer   syt view  (HTML) │            │  (provider sees ciphertext only)│
   └──────────────────────────────┘            └─────────────────────────────────┘
```

## The pieces, and what each depends on

| Piece | File(s) | Job | Depends on |
|-------|---------|-----|-----------|
| **Connectors** | `connectors/*.py` | Parse one platform's official export into canonical records. One file each; a community PR adds a platform without touching anything else. | `models.py` only |
| **Engine** | `engine.py` | Detect platform → keep the raw snapshot → parse → store. `ingest_folder` scans a whole folder. | connectors, store |
| **Models** | `models.py` | The canonical `NormalizedRecord` / `MediaRef` / `Batch` every connector emits. | — |
| **Archive** | `store/` | Owns the blob store, the SQLite/FTS5 index, and the manifests; dedups and indexes each batch. | crypto |
| **Crypto** | `crypto/` | AES-256-GCM at rest; master key wrapped by your passphrase; Recovery Kit; OS-keychain cache. | — |
| **Status** | `status.py` | Counts + the dead-man's-switch ("no successful backup in N days"). | store |
| **CLI** | `cli.py` | `init / ingest / serve / status / search / view / sync / schedule / doctor / …`. | everything |
| **Web app** | `webapp.py` | Loopback-only chat UI: dashboard, conversations, search. Pure router + 4 JSON endpoints. | store, status |
| **Viewer** | `viewer.py` | One self-contained offline HTML file of the whole archive. | store |
| **Sync** | `sync/` | restic (versioned encrypted snapshots) + rclone (mirror to your cloud). Optional binaries. | — |
| **Scheduler** | `scheduler.py` | Generate launchd/cron/schtasks entries for periodic runs. | — |

Each unit has one job and a narrow interface, so you can understand or replace any
one without reading the others — and the test suite covers each in isolation.

## The simplest possible trigger

The whole flow reduces to **"put exports in a folder, run one command"**:

```bash
syt ingest ~/Downloads --all      # ingests every recognizable export it finds
```

- **Manual:** run it whenever you download fresh exports. Idempotent — re-running
  never duplicates (records dedup by a stable id), so it's safe to run any time.
- **Automatic:** hand that same command to your OS scheduler once:

  ```bash
  syt schedule --every daily       # or add the printed cron/schtasks line
  ```

That is the entire "motion": **download → drops into a folder → a scheduled
`ingest --all` picks it up → it appears in the app.** No daemon, no server, no
account. The heavier live-fetch connectors (Phase 2) slot into the *same* engine
behind the *same* connector interface — they just replace the manual download step
with an API/session pull, and everything downstream (store, search, chat UI, sync)
is unchanged.

## Why it's shaped this way

- **Local-first is the architecture, not a feature.** Because everything runs under
  your own credentials on your own machine, there is no server to trust, no data
  processor, and nothing to breach.
- **The connector is the only thing that knows about a platform.** Storage,
  encryption, search, the UI, and sync are all platform-agnostic, so adding
  platforms stays cheap and the blast radius of a platform changing its format is
  one file.
- **The index is derived, not the source of truth.** The raw snapshots + manifests
  are authoritative; the SQLite index can always be rebuilt from them.
