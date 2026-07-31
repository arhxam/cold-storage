// electron-builder afterPack hook: ad-hoc sign the whole app bundle.
//
// We ship unsigned (no Apple Developer ID yet), but on Apple Silicon a binary
// with a broken/absent signature is killed outright. An ad-hoc signature
// (`codesign -s -`) makes a locally-built app run fine; a *downloaded* copy
// additionally needs its quarantine flag cleared once (see docs/BUILDING-APP.md).

const { execFileSync } = require("child_process");
const path = require("path");

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") return;
  const appName = `${context.packager.appInfo.productFilename}.app`;
  const appPath = path.join(context.appOutDir, appName);
  // --deep is fine for a plain ad-hoc signature (no entitlements to preserve).
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", appPath], {
    stdio: "inherit",
  });
  execFileSync("codesign", ["--verify", "--deep", "--strict", appPath], {
    stdio: "inherit",
  });
  console.log(`  • ad-hoc signed ${appName}`);
};
