# Launch guide — test it, record it, ship it

Everything here assumes the built DMG from `app/release/`. Run
`./app/verify-dmg.sh` first: 12 checks, all must pass.

---

## Part 1 — Test it yourself (about 20 minutes)

Do this on your own Mac, with your own accounts, before anyone else sees it.

### Setup, from a real download

1. Delete any old install: drag **Cold Storage** from Applications to the Trash.
   Leave `~/ColdStorage` alone — you want to prove upgrades work.
2. Open the DMG, drag the app to Applications, open it from **Applications**
   (not from the disk image).
3. **Expect:** no Gatekeeper warning, no right-click. If you see *"cannot be
   opened because the developer cannot be verified"*, stop — that's a signing
   problem, tell me.
4. First run only: a Recovery Kit dialog. **Check your Desktop** — there should be
   a file called `Cold Storage — Recovery Kit.txt`. It must NOT be inside
   `~/ColdStorage`.

### The core loop

5. **Accounts → Connect** on one platform. Reddit, LinkedIn or X are fastest;
   Instagram is the most representative.
6. Sign in on the platform's own page. Close the window when you land on your feed.
   **Expect:** the row flips to *Connected*, then *Working*, within ~30 seconds.
7. Set the frequency dropdown to **Every day**.
8. **Expect** within a few minutes: *Preparing* — the platform is building your
   archive. That genuinely takes hours. Leave it.
9. Come back later. **Expect:** *Downloading* → *Backing up* → *Backed up*, and
   the platform appears in the sidebar with a real item count.

### What to check once data lands

10. Click the platform → click a conversation. Your real messages, in order,
    newest at the bottom.
11. Click **Photos & video**. Your actual photos, decrypted from the archive.
    Click one — the lightbox opens; arrow keys page through; Escape closes.
12. Search something you remember saying. Include a question mark — `why?` used
    to crash it.
13. Close the window. **Expect:** the app stays in the menu bar and keeps working.

### The things most likely to be broken

| Test | How | Expect |
|---|---|---|
| **Reboot** | Restart your Mac | App returns on its own, menu bar only, no window |
| **Crash recovery** | Force-quit mid-backup, reopen | It resumes and finishes |
| **Downloads** | Drop any export `.zip` in `~/Downloads` | Backed up on its own; your file is *not* deleted |
| **Bad file** | Drop an Instagram **HTML** export in | Says "re-download choosing JSON", doesn't retry forever |
| **Expired login** | Log out of the platform in Safari, hit *Back up now* | Says *Reconnect*, and Reconnect actually re-prompts |
| **Re-ingest** | Add the same export twice | Second time adds 0 items — no duplicates |

If anything misbehaves: **Archive → Show Error Log** (or the tray menu) and send
me that file plus which build (tray shows the version).

---

## Part 2 — Recording the video

### Before you hit record

- **Use a throwaway archive**, not your real one, unless you're happy showing your
  actual DMs on the internet:
  ```bash
  export COLD_HOME=/tmp/demo
  open -a "Cold Storage"
  ```
  Or blur in post. Your call — but decide *before* recording, not after.
- Set your Mac to **dark mode**. The app is dark; a light menu bar looks wrong.
- Hide desktop clutter, turn off notifications (Focus → Do Not Disturb).
- Record at **1280×800** or larger, 60fps if you can.
- Have one account **already connected and backed up** before recording. Nobody
  wants to watch a four-hour export get prepared.

### The 60-second cut

The story is *"the backup has to already exist"* — lead with the fear, then the
relief.

| Time | On screen | Say |
|---|---|---|
| 0:00–0:07 | A platform's "account suspended" page, or just the app's empty state | "If you get banned tomorrow, the download-your-data button stops working too." |
| 0:07–0:15 | **Accounts** page, click **Connect** on Instagram | "So you connect your accounts once." |
| 0:15–0:22 | Real Instagram login appears, you sign in | "You sign in on Instagram's own page. The password never touches this app." |
| 0:22–0:30 | Row flips to Connected; set frequency to **Every day** | "Pick how often. That's the whole setup." |
| 0:30–0:40 | Cut to a populated archive — Overview with item counts | "From then on it requests your export, downloads it, and files it away. On its own." |
| 0:40–0:50 | Open a conversation, scroll, open **Photos & video**, click a photo | "Your DMs. Your photos. All of it, on your Mac." |
| 0:50–0:57 | Search box, type a phrase, results appear | "Searchable offline, forever." |
| 0:57–1:00 | Close the window, point at the menu bar icon | "Nothing leaves your machine. It's yours." |

### Shots worth getting

- The **Connect → real platform login** moment. That's the "wait, it actually does
  it?" beat, and it's the whole product.
- The **frequency dropdown**. It's what makes it automatic rather than another
  manual chore.
- **Photos & video** filling in. Most convincing single frame you have.
- The **menu bar icon** after closing the window — proves it keeps running.

### Don't

- Don't show `~/ColdStorage` file listings; it's boring and it shows real paths.
- Don't show the Recovery Kit contents on camera. That's a real key.
- Don't record a first-run where the export takes hours. Pre-bake it.

---

## Part 3 — Posting it

The hook that works is the specific fear, not the feature list:

> Platforms can ban you with no warning — and when they do, "Download your data"
> stops working too. The backup has to already exist.
>
> So I built Cold Storage. Connect your accounts once; it requests your official
> export, downloads it, and keeps an encrypted, searchable copy on your Mac. On a
> schedule. Nothing ever leaves your machine.
>
> Free, open source, signed for macOS: github.com/arhxam/cold-storage-social-media-backup

Be honest about the limits in a reply — it earns more trust than it costs:

> Automatic for Instagram, Facebook, X, Google, Snapchat, LinkedIn and Reddit.
> Discord/Telegram/WhatsApp/Slack have no automatable web export, so those you
> still add by hand. Apple Silicon Macs only for now.

### Answers to the questions you'll get

- **"Does it have my password?"** No. You sign in on the platform's own page, in
  a window that belongs to the app; the session cookie stays on your Mac and the
  password never passes through any of our code.
- **"Will this get me banned?"** It uses the official data-export flow every
  platform is legally required to offer — the same clicks you'd make yourself. No
  scraping, no unofficial APIs.
- **"Is my data encrypted?"** Media, yes — AES-256 with a key wrapped by your
  passphrase. The search index and raw exports aren't yet; they rely on your Mac's
  account and FileVault. It's written down plainly in the README, and full
  index encryption is next.
- **"Where does it go?"** `~/ColdStorage` on your machine. There is no server.
  Optional cloud sync is end-to-end encrypted through restic, so your provider
  sees ciphertext only.
- **"Windows/Linux?"** The engine (`cold`) already runs anywhere Python does. The
  desktop app is Mac-only for now.

### Before you post

- [ ] `./app/verify-dmg.sh` — 12/12
- [ ] Downloaded the DMG from the GitHub release link and opened it, on a Mac that
      hasn't seen the app before if you can
- [ ] README renders correctly on github.com with all four screenshots
- [ ] The release link in the README points at the right file
- [ ] You know which build you're shipping (tray → version)
