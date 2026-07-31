# Building the macOS app (DMG)

The Mac app is a thin Electron shell around a PyInstaller-frozen `syt` engine.
Apple Silicon only; needs `uv`, Node, and npm.

## Quick local build (unsigned)

Three commands rebuild an ad-hoc-signed DMG that runs on the machine that built
it:

```sh
# 1. Freeze the Python engine into dist/syt/ (onedir bundle)
uv run pyinstaller packaging/syt.spec --noconfirm

# 2. Install the Electron shell's dev dependencies
cd app && npm install

# 3. Build the DMG (output: app/release/Save Your Shit-<version>-arm64.dmg)
npm run dist
```

Without a signing identity the engine binaries and the outer bundle are ad-hoc
signed (`codesign -s -`). That is enough to launch a locally-built app, but a
**downloaded** copy is quarantined by Gatekeeper and needs a right-click → Open
(or `xattr -d com.apple.quarantine "…app"`). Use the signed flow below for
anything you hand to other people.

## Signed + notarized release build (for distribution)

`app/release.sh` does the whole thing — inside-out Developer ID signing,
DMG signing, notarization, stapling, and a Gatekeeper check:

```sh
uv run pyinstaller packaging/syt.spec --noconfirm   # freeze the engine first
cd app && npm install                               # once
./release.sh
```

The result is a DMG that opens with no warnings and no quarantine bypass.

### One-time signing setup

1. A **Developer ID Application** certificate in your login keychain
   (Xcode → Settings → Accounts → Manage Certificates, or download from the
   Apple Developer portal). `release.sh` auto-detects it; override with
   `SYT_SIGN_IDENTITY`.
2. A **notarytool keychain profile** created from an App Store Connect API key:

   ```sh
   xcrun notarytool store-credentials "syt-notary" \
     --key ~/.appstoreconnect/private_keys/AuthKey_XXXX.p8 \
     --key-id XXXX --issuer <issuer-uuid>
   ```

   `release.sh` uses the `syt-notary` profile by default; override with
   `SYT_NOTARY_PROFILE`.

## How signing works

- `app/after-pack.js` signs every Mach-O inside the frozen `syt` engine
  **inside-out** with the hardened runtime and `entitlements.mac.plist`, before
  electron-builder seals the outer bundle. With no `SYT_SIGN_IDENTITY` it falls
  back to ad-hoc signatures.
- `entitlements.mac.plist` grants the JIT / library-validation exceptions
  Electron's V8 and the bundled Python dylibs need under the hardened runtime.
- `release.sh` signs the disk image itself as well — an unsigned DMG is flagged
  by Gatekeeper even when the app inside is notarized.

## Notes

- The PyInstaller spec (`packaging/syt.spec`) bundles the `keyring` macOS
  Keychain backend as a hidden import so the app can cache the encryption key.
- If `npm install` leaves `app/node_modules/electron/dist` tiny/incomplete
  (extract-zip can fail silently on newer Node), extract the cached zip
  manually:

  ```sh
  cd app
  ditto -x -k ~/Library/Caches/electron/*/electron-v*-darwin-arm64.zip node_modules/electron/dist
  printf 'Electron.app/Contents/MacOS/Electron' > node_modules/electron/path.txt
  ```
