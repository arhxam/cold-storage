# Save Your Shit — Design Spec

> A local-first tool that continuously backs up your own social-media data (chats,
> photos, followers, posts) to your own machine and your own cloud, so a platform
> ban can never erase your history. **No server. Nothing leaves your device except
> to storage you control.**

Status: **Phase 1 implemented** (engine + `syt` CLI + 4 connectors; see below)
Date: 2026-07-31
Working title: *Save Your Shit* (rename candidate before public launch)

> **Build status.** Phase 1 is implemented and tested (`src/saveyourshit`, 46 tests,
> CI on Linux/macOS/Windows × Py3.11/3.12): the encrypted content-addressed store,
> AES-256 key custody + Recovery Kit, the connector framework, Rail-A export
> parsers for **Instagram, Facebook/Messenger, Discord, X**, the `syt` CLI
> (`init/ingest/status/search/view/where/connectors/passphrase/recover`), the
> dead-man's-switch staleness check, and an offline HTML viewer. Phases 2–3 (live
> API connectors, cloud sync, desktop app, browser extension) remain as designed
> below.

---

## 1. Why this exists

Platforms (Instagram, Facebook, Discord, X, Slack, …) can ban an account with no
warning and no chance to export. When that happens, the platform's own "Download
your data" link **also stops working** — it redirects to a login you no longer
have. The only defense is a backup that *already happened*, on hardware you own.

This tool is that second layer of backup. It runs locally, on a schedule, using the
user's own logged-in sessions and their own official data-export rights, and stores
an encrypted, browsable archive on their disk and (optionally) in their own cloud.

### Non-negotiable principles

1. **Local-first, zero-server.** There is no backend we operate. All processing,
   credentials, and data stay on the user's machine. The only network egress is
   (a) to the platforms themselves and (b) to the user's *own* cloud storage.
2. **The user owns and can locate their data.** The app always shows exactly where
   files live on disk and can reveal them in Finder/Explorer.
3. **Safety over completeness.** We prefer official export rails (ToS-safe) over
   session scraping (ban risk). Anything risky is opt-in, clearly labeled, and off
   by default — because the whole point is *not* getting the account banned.
4. **Open source, trivially installable.** A download button on GitHub for
   non-technical users; a one-command path for technical users. No account signup.

---

## 2. The three "rails" every connector uses

The single most important design fact: there is no one way to get data. Each
platform is reached by one of three rails, and the framework must support all three.

| Rail | What it is | Ban risk | Automatable |
|------|------------|----------|-------------|
| **A — Official export** | Trigger the platform's own "Download your data," then race to fetch the ZIP before its link expires. | Very low | Semi (one login; then automated trigger + fetch) |
| **B — Official API** | OAuth/token APIs for your own data (Telegram MTProto, Slack, Reddit, Google, YouTube). | Very low | Fully, after one consent |
| **C — Session/private tools** | instaloader-class tools riding the logged-in session for data the official rails omit (e.g. fresh stories, follower identities). | Medium–high | Yes, but fragile & opt-in |

**Design consequence:** the product's flagship, lowest-risk capability is Rail A
done well — **detect that an export is ready → download it before the link dies →
verify → store encrypted.** Export links expire fast (Meta ~4 days, X ~7 days,
LinkedIn 72h, Snapchat memory URLs ~7 days, Discord 30 days), so this
"expiry-racing" scheduler is genuinely valuable and near-zero risk.

---

## 3. Architecture

**One engine, two faces, one optional harvester.** Everything runs on-device.

```
┌─────────────────────────────────────────────────────────────────┐
│  ELECTRON APP (the face)          │  CLI (same engine, headless)  │
│  • Connections dashboard          │  • `sys run`, `sys login …`   │
│  • Data viewer (chats/followers)  │  • for power users / Docker   │
│  • "where is my data" panel       │                               │
└───────────────┬───────────────────┴───────────────┬───────────────┘
                │  local stdio JSON-RPC              │  direct calls
                ▼                                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  ENGINE (Python, frozen as a local sidecar)                      │
│  ┌───────────┐  ┌────────────┐  ┌───────────┐  ┌──────────────┐  │
│  │ Scheduler │  │ Connector  │  │ Normalizer│  │ Store /       │  │
│  │ (expiry-  │  │ registry   │  │ → SQLite  │  │ encryption /  │  │
│  │  aware)   │  │ (A/B/C)    │  │  FTS5     │  │ cloud sync    │  │
│  └───────────┘  └─────┬──────┘  └───────────┘  └──────┬───────┘  │
│                        │                               │           │
│         Playwright (controlled Chromium profile)   restic + rclone │
│         OS keychain (tokens/keys)                  (binaries)      │
└───────────────┬───────────────────────────────────────────────────┘
                │  native messaging (local IPC, no network)
                ▼
┌─────────────────────────────────────────────────────────────────┐
│  BROWSER EXTENSION (optional harvester, Phase 3)                  │
│  Rides the user's REAL logged-in tab to trigger exports & pass    │
│  the session to the engine — lowest ban risk, no session theft.   │
└─────────────────────────────────────────────────────────────────┘
```

### Why these choices

- **Engine in Python.** The mature export ecosystem (instaloader, Telethon,
  wa-crypt-tools, snscrape, google-api libs, praw) is Python. The open-source
  headless path is naturally `pipx install` / Docker. The Electron app bundles the
  engine frozen with PyInstaller and spawns it as a child process, talking over
  **stdio JSON-RPC** — a child process, *not* a server, so "no server" holds.
- **Electron for the app**, not Tauri: this app is login- and webview-heavy, and
  Electron's session/cookie/auto-update tooling is mature. Bundle size is
  acceptable for a backup product; mitigate by downloading Chromium on first run.
- **Playwright** for Rail A automation, launched as its own **controlled Chromium
  profile** with a one-time login. (Reusing the user's existing Chrome is
  impossible as of Chrome 136+ / Meta app-bound cookie encryption — the extension
  is the only way to act inside the real session, hence Phase 3.)
- **restic** = encrypted, deduplicated, versioned local snapshots. **rclone** =
  sync those snapshots to the user's own cloud (B2/S3 native; Drive/Dropbox/OneDrive/
  WebDAV via rclone's OAuth). We drive both as bundled binaries — we do not
  reimplement crypto, dedup, or 70 cloud integrations.
- **OS keychain** (macOS Keychain / Windows Credential Manager / libsecret) holds
  tokens and the master encryption key; bulk sessions are encrypted at rest.

### The connector interface (the extensibility spine)

Each platform is one module implementing a narrow contract. The host owns storage,
encryption, scheduling, dedup, and sync so connectors stay small (community can add
one in a single file).

```python
class Connector(Protocol):
    id: str                     # "instagram", "discord", "slack"
    display_name: str
    rail: Literal["export", "api", "session"]
    provides: list[str]         # ["dms","followers","posts","media",...]

    def authenticate(self, ctx: AuthContext) -> Session: ...
        # opens a real (headed) browser or OAuth flow once; returns a
        # reusable session the host persists in the OS keychain.

    def check_session(self, s: Session) -> Literal["valid","expired","checkpoint"]: ...

    def run(self, ctx: RunContext, cursor: Cursor | None) -> Iterable[Batch]: ...
        # incremental pull. For Rail A: trigger export, poll, fetch ZIP before
        # link expiry, unpack. Yields normalized records + media *references*
        # (host fetches, hashes, dedups). Returns next cursor (watermark).
```

---

## 4. Connectors shipped at launch

Named priority set (Instagram, Facebook, Discord, X, Slack) plus the clean
high-value ones. Each tagged by rail and honest about what it can and can't get.

| # | Connector | Rail | Gets | Key caveat |
|---|-----------|------|------|------------|
| 1 | **Instagram** | A (+ opt-in C) | DMs, followers/following, posts, stories, saved, likes | Export JSON is **mojibake-encoded — must repair** (`s.encode('latin-1').decode('utf-8')`). 4-day link. IG E2EE sunset (May 2026) makes DMs export-eligible again. |
| 2 | **Facebook + Messenger** | A | Timeline, friends, Messenger history, media | Messenger **E2EE chats need Secure Storage enabled + user PIN** — onboarding must check this or history is silently missing. |
| 3 | **Discord** | A only | Servers, messages you sent | Official package = **your half of DMs only**; token automation is ban/crackdown risk (March 2026) → **not enabled**. State the limitation plainly. |
| 4 | **X / Twitter** | A only | Tweets, media, likes, followers, **full DM text** | API is dead for consumers (pay-per-use / $42k enterprise). Archive link **expires 7 days** — race it. |
| 5 | **Slack** | B | Your messages, DMs, files across workspaces | Uses a **user OAuth token**; per-workspace. Free-plan history limits are the platform's, not ours. |
| 6 | **Telegram** | B | Full chat history + media, all your dialogs | **Gold standard** — Telethon/MTProto, officially sanctioned, zero ban risk. Respect FloodWait (single-threaded). |
| 7 | **Google / YouTube** | A + B | Takeout (mail, drive, photos, watch/search history) + Data Portability + YT Data API | Reference-quality: supports **recurring + incremental** exports. Lowest-risk demo connector. |
| 8 | **Reddit** | B | Posts, comments, saved, votes, messages | Free OAuth API for own history; export for >1000-item history. |
| 9 | **WhatsApp** | C-local | **Full local message DB** (highest fidelity DMs) | Parse the user's own `msgstore.db.crypt15` with their 64-hex backup key (or iOS local backup). Zero server contact. |
| 10 | **Snapchat** | A | Account data, saved chats, **Memories** | Memories ZIP contains **expiring download URLs (~7 days)** — must fetch immediately. Ephemeral chats are already gone. |

Connectors 1–6 are the "named" priority; 7–10 round out coverage and prove all
three rails. Post-launch community additions (LinkedIn, TikTok, Spotify, iCloud
Photos via `icloudpd`, Pinterest) drop in through the same interface.

---

## 5. Storage, encryption & cloud sync

```
~/SaveYourShit/
├── index.sqlite            # searchable index (FTS5), job history, cursors
├── config.toml             # enabled connectors, schedules, cloud remotes
├── blobs/sha256/ab/cd/…    # content-addressed media (dedup across posts/exports)
├── instagram/
│   ├── snapshots/2026-07-31/   # raw official export ZIPs, unpacked, immutable
│   ├── manifest.jsonl          # append-only, diffable normalized records
│   └── cursors.json
├── facebook/ …             # same shape per connector
└── .runs/2026-07-31.json   # per-run report: counts, errors, expiry events
```

- **Two tiers:** keep the **raw export bytes verbatim** (future-proof, re-importable)
  *and* a **normalized copy** in SQLite/FTS5 for search and the viewer.
- **Encryption:** the whole archive is snapshotted into a **restic repository**
  (client-side AES-256, deduplicated, versioned). Passphrase → scrypt; the derived
  key is cached in the **OS keychain** so scheduled runs don't prompt.
- **Cloud sync (optional):** restic pushes snapshots to the user's own **Backblaze
  B2 / S3** natively, or to **Google Drive / Dropbox / OneDrive / WebDAV** via
  bundled **rclone**. The cloud provider only ever sees ciphertext.
- **Integrity:** post-backup `restic check`; periodic `--read-data`; a
  **dead-man's-switch alert** ("no successful backup in N days") since silent
  scheduler failure is the top real-world cause of data loss.

### ⚠️ The one existential risk: losing the encryption key

Client-side encryption means a forgotten passphrase + lost machine = **permanently
unrecoverable backup**. Mitigations, all shipped:
1. A printable/QR **Recovery Kit** generated at setup; the user must confirm they
   saved it before the first backup completes.
2. Key cached in OS keychain for daily use (passphrase only needed for recovery).
3. Optional **Shamir secret-sharing** (split key across trusted locations) for
   advanced users.

---

## 6. The app experience

### Screen 1 — Connections dashboard (the cards)

A grid of platform cards, each with a live status:
- 🟢 **Backing up** — "last backup 2h ago · next in 22h · 4,182 items"
- 🟡 **Needs attention** — "session expired, click to re-login" / "export ready — fetching now"
- ⚪ **Not connected** — one-click connect
- 🔴 **Stale** — "no successful backup in 9 days" (dead-man's-switch)

Every card links to **exactly where that data lives on disk** with a "Reveal in
Finder/Explorer" button. A top-level banner shows the archive's total size,
last-verified time, and cloud-sync status.

### Screen 2 — Data viewer (turns a backup into a product)

Because it's just local structured data, browse it beautifully, offline, forever:
- **Followers/following** — searchable, with **diffs over time** ("you lost 3
  followers this week"; "40 accounts you follow went inactive") — something the
  platforms won't show you.
- **DMs/chats** — full-text search across your entire history (SQLite FTS5).
- **Media gallery** — photos/videos with original EXIF (dates, GPS) preserved.
- **Posts / saved / likes** — browsable archives per platform.

The viewer is the differentiator. The export tools exist; **nobody wraps them in a
dashboard + searchable, diffable, offline UI that is permanently yours.**

---

## 7. Setup & distribution (the "download button")

Two audiences, both easy:

- **Non-technical → GitHub Releases.** Signed installers per OS — `.dmg` (macOS,
  notarized), `.exe`/NSIS (Windows, code-signed), `.AppImage`/`.deb` (Linux). The
  README has a prominent **Download** button/badge pointing at the latest release.
  Double-click, open, connect a platform, done.
- **Technical → one command.** `pipx install saveyourshit` (or `uvx saveyourshit`)
  and a `docker compose up` for the headless engine + `config.toml`. Same engine,
  no UI.

First-run wizard: pick a backup folder → set a passphrase → **save Recovery Kit** →
connect first platform (a real browser login opens once) → schedule (default daily).

---

## 8. Security, privacy & legal posture

- **Local-first is the legal architecture, not just a feature.** Because the
  software runs on the user's machine under the user's own credentials on the
  user's own data, we are a tool vendor, not a data processor — no GDPR-controller
  status, no third-party privacy harm.
- **Frame as portability-rights tooling** (GDPR Art. 20 / DMA Art. 6(9)), not
  scraping. Default to Rails A + B; gate Rail C behind an explicit, plain-language
  ban-risk warning, off by default.
- **Never proxy credentials or data through any server we run** (we run none).
  Tokens live in the OS keychain; cloud copies are ciphertext.

---

## 9. Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| Session tools get the account **banned** (the irony) | Rails A/B default; Rail C opt-in, throttled, warned; reuse persisted real sessions, never bulk-crawl. |
| **Connector rot** (platforms change markup/flows) | Thin one-file connector interface; Rail A official flows rot slowest; community fixes cheap. |
| Export **link expires** before we fetch | Expiry-aware scheduler races the download; email fallback (Gmail/IMAP) when the platform doesn't show an in-page download. |
| User **loses encryption key** | Recovery Kit (forced at setup), keychain cache, optional Shamir. |
| **Silent scheduler failure** | Dead-man's-switch alert; per-run reports; catch-up-on-wake. |
| Bundled **Chromium/binaries** size & signing | Download Chromium on first run; auto-updater; notarize/sign in CI. |
| Meta **mojibake** / Messenger E2EE gaps | Encoding-repair pass on import; onboarding checks Secure Storage + PIN. |

---

## 10. Build phases

Each phase is independently useful and open-sourceable. Nothing ever needs a server.

- **Phase 1 — Engine + CLI core.** Connector framework, scheduler, restic store,
  keychain, local + B2/S3 sync, and connectors for **Instagram, Facebook, Telegram**
  (one of each rail). Proves the hard part end-to-end. Ships as `pipx`/Docker.
- **Phase 2 — Electron app.** Dashboard + "where's my data" + data viewer over the
  Phase-1 data. Add **X, Discord, Slack, Google, Reddit** connectors. GitHub
  Release installers + download button.
- **Phase 3 — Browser extension.** In-session harvester via native messaging for
  the lowest-ban-risk export triggering; add **WhatsApp (local DB)** and **Snapchat**.

---

## 11. Tech stack (summary)

| Concern | Choice |
|---------|--------|
| Engine | Python 3.12, frozen with PyInstaller as an Electron sidecar; also `pipx`/Docker |
| App | Electron + TypeScript (React); stdio JSON-RPC to the engine |
| Browser automation | Playwright (controlled Chromium profile) |
| Export libs | instaloader, Telethon, praw, google-api-python-client, slack-sdk, wa-crypt-tools |
| Backup/versioning | restic (bundled binary) |
| Cloud sync | rclone (bundled binary) |
| Secrets | OS keychain via `keyring` |
| Index/search | SQLite + FTS5 |
| Extension (Ph3) | Chrome/Firefox MV3 + native messaging |

---

## 12. Open questions to confirm before implementation

1. **Public name** — keep "Save Your Shit" or rename for the store/README?
2. **Primary OS for Phase 1** — Mac-first, or Mac+Windows together?
3. **Cloud default** — Backblaze B2 first (cheap, no OAuth), with Drive/Dropbox via
   rclone as follow-ups? Confirm.
4. **Rail C default stance** — ship instaloader-style connectors disabled-by-default
   with a warning (recommended), or not at launch?
```
