// electron-builder afterPack hook: sign the bundled Python engine.
//
// electron-builder signs the app bundle itself, but the PyInstaller `syt`
// bundle we drop into Resources/ contains its own Mach-O binaries and dylibs.
// Those must be signed *before* the outer bundle is sealed (inside-out order),
// with the hardened runtime and entitlements, or notarization rejects the app.
//
// With no Developer ID (SYT_SIGN_IDENTITY unset) we fall back to an ad-hoc
// signature: enough for a locally-built app to run, not enough to distribute.

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const IDENTITY = process.env.SYT_SIGN_IDENTITY || null;

function isMachO(file) {
  try {
    const fd = fs.openSync(file, "r");
    const buf = Buffer.alloc(4);
    fs.readSync(fd, buf, 0, 4, 0);
    fs.closeSync(fd);
    const magic = buf.readUInt32BE(0);
    // 32/64-bit Mach-O (both endiannesses) and universal binaries
    return [0xfeedface, 0xfeedfacf, 0xcefaedfe, 0xcffaedfe, 0xcafebabe].includes(magic);
  } catch {
    return false;
  }
}

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(p);
    else if (entry.isFile()) yield p;
  }
}

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") return;

  const appName = `${context.packager.appInfo.productFilename}.app`;
  const appPath = path.join(context.appOutDir, appName);
  const engineDir = path.join(appPath, "Contents", "Resources", "syt");
  const entitlements = path.join(__dirname, "entitlements.mac.plist");

  if (fs.existsSync(engineDir)) {
    // Sign every Mach-O inside the engine bundle, deepest paths first.
    const binaries = [...walk(engineDir)].filter(isMachO).sort((a, b) => b.length - a.length);
    const args = (target) =>
      IDENTITY
        ? [
            "--force",
            "--timestamp",
            "--options",
            "runtime",
            "--entitlements",
            entitlements,
            "--sign",
            IDENTITY,
            target,
          ]
        : ["--force", "--sign", "-", target];

    for (const bin of binaries) {
      execFileSync("codesign", args(bin), { stdio: "pipe" });
    }
    console.log(
      `  • signed ${binaries.length} engine binaries ` +
        (IDENTITY ? `with "${IDENTITY}"` : "ad-hoc (no Developer ID)")
    );
  }

  // Without a Developer ID electron-builder won't sign the outer bundle either,
  // so ad-hoc sign it here to keep the app launchable on Apple Silicon.
  if (!IDENTITY) {
    execFileSync("codesign", ["--force", "--deep", "--sign", "-", appPath], { stdio: "inherit" });
    execFileSync("codesign", ["--verify", "--deep", "--strict", appPath], { stdio: "inherit" });
    console.log(`  • ad-hoc signed ${appName}`);
  }
};
