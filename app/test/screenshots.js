// Capture README screenshots from the real UI.
//
//   SYT_URL=http://127.0.0.1:PORT OUT=docs/screenshots npx electron app/test/screenshots.js
//
// Loads the actual served app in a fixed-size window and writes PNGs. Using the
// real UI (rather than a mock) means the screenshots cannot drift from what
// ships. `SYT_FAKE_ACCOUNTS=1` injects a plausible Accounts state so that page
// can be shown without connecting anyone's real social accounts.

const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");

const URL = process.env.SYT_URL || "http://127.0.0.1:8787/";
const OUT = process.env.OUT || "docs/screenshots";
const W = 1280;
const H = 820;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shoot(win, name) {
  await sleep(900);
  const img = await win.webContents.capturePage();
  const file = path.join(OUT, name + ".png");
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(file, img.toPNG());
  console.log("wrote", file);
}

app.whenReady().then(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const win = new BrowserWindow({
    width: W,
    height: H,
    show: false,
    backgroundColor: "#09090b",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // Supplies an inert window.sytBridge so the UI renders its desktop-app
      // chrome. It must exist before the page script runs, hence a preload.
      preload: path.join(__dirname, "screenshot-preload.js"),
    },
  });

  await win.loadURL(URL);
  await sleep(2200);
  await shoot(win, "dashboard");

  // Accounts page, driven through the app's own render path.
  await win.webContents.executeJavaScript(`(() => { showConnect(); return true; })()`, true);
  await shoot(win, "accounts");

  // A conversation, from the real archive.
  const opened = await win.webContents.executeJavaScript(
    `(async () => {
       const c = (STATUS.connectors || []).find(c => c.connector === 'instagram');
       if (!c) return false;
       await openConnector('instagram');
       return true;
     })()`,
    true
  );
  if (opened) {
    await sleep(1600);
    await shoot(win, "chat");
  }

  win.destroy();
  app.exit(0);
});
