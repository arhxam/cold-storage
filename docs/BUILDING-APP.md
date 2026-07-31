# Building the macOS app (DMG)

The Mac app is a thin Electron shell around a PyInstaller-frozen `syt` engine.
Three commands rebuild the DMG from a clean checkout (needs `uv`, Node, npm;
Apple Silicon only):

```sh
# 1. Freeze the Python engine into dist/syt/ (onedir bundle)
uv run pyinstaller packaging/syt.spec --noconfirm

# 2. Install the Electron shell's dev dependencies
cd app && npm install

# 3. Build the DMG (output: app/release/Save Your Shit-<version>-arm64.dmg)
npx electron-builder --mac --arm64
```

Notes:

- The PyInstaller spec (`packaging/syt.spec`) bundles the `keyring` macOS
  Keychain backend as a hidden import so the app can cache the encryption key.
- `app/after-pack.js` ad-hoc signs the packaged `.app` (`codesign -s -`).
  We have no Apple Developer ID yet, so the app is **unsigned**:
  - A locally-built app runs fine.
  - A **downloaded** DMG is quarantined by Gatekeeper. First open is
    right-click → Open, or clear the flag once:

    ```sh
    xattr -d com.apple.quarantine "/Applications/Save Your Shit.app"
    ```

  - Real distribution needs a proper Apple Developer ID signature +
    notarization; swap `identity: null` in `app/package.json` for a Developer
    ID certificate when we have one.
- If `npm install` leaves `app/node_modules/electron/dist` tiny/incomplete
  (extract-zip can fail silently on newer Node), extract the cached zip
  manually:

  ```sh
  cd app
  ditto -x -k ~/Library/Caches/electron/*/electron-v*-darwin-arm64.zip node_modules/electron/dist
  printf 'Electron.app/Contents/MacOS/Electron' > node_modules/electron/path.txt
  ```
