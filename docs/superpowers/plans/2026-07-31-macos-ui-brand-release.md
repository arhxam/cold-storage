# macOS UI, Brand, and Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a cohesive blue-branded macOS app, exhaustively exercise its user-facing states, and publish a Developer ID-signed and notarized DMG.

**Architecture:** Keep the Python local web UI and Electron shell boundaries intact. Add deterministic vector/raster brand assets, finish the embedded UI design system, and use the existing inside-out signing hook plus release script for notarized distribution.

**Tech Stack:** Python 3.12, stdlib HTML/CSS/JavaScript, pytest, Electron 33, electron-builder, PyInstaller, macOS codesign/notarytool/stapler.

---

### Task 1: Lock the brand contract with tests

**Files:**
- Create: `tests/test_brand_assets.py`
- Modify: `tests/test_webapp_ui.py`

- [ ] Add failing assertions for the blue brand token, inline vector mark hook, SVG source, PNG sizes, ICNS bundle icon, and Electron package icon configuration.
- [ ] Run `uv run pytest tests/test_brand_assets.py tests/test_webapp_ui.py -q` and confirm the missing assets/hooks fail.

### Task 2: Create and wire the brand assets

**Files:**
- Create: `assets/logo/save-your-shit-mark.svg`
- Create: `assets/logo/save-your-shit-mark-{16,32,64,128,256,512,1024}.png`
- Create: `app/assets/icon.icns`
- Modify: `src/saveyourshit/webapp.py`
- Modify: `app/package.json`
- Modify: `README.md`

- [ ] Draw the deterministic cobalt shield/archive SVG with a safe-area viewBox and no external dependencies.
- [ ] Render the PNG family and assemble the ICNS using macOS tooling.
- [ ] Replace the amber UI accent/mark with the blue brand system and reuse the vector geometry in the embedded UI.
- [ ] Configure electron-builder to use the ICNS file and add the SVG to README presentation.
- [ ] Re-run the focused tests until green.

### Task 3: Complete UI-state verification

**Files:**
- Modify: `src/saveyourshit/webapp.py` only for defects reproduced during verification.
- Modify: `tests/test_webapp_ui.py` before each behavior fix.

- [ ] Run the complete Python suite and Ruff.
- [ ] Seed a disposable multi-platform archive and open it with the local server.
- [ ] Exercise dashboard, platform navigation, conversation selection, search hit/no-result, followers/following, posts/saved, empty archive, and narrow/standard/wide viewports.
- [ ] Check DOM structure, focusability, console errors, overflow, and reduced-motion behavior.
- [ ] For every defect, add a failing regression assertion, implement the smallest fix, and re-run focused and full tests.

### Task 4: Build and verify the native app

**Files:**
- Modify: `app/main.js`, `app/after-pack.js`, or `app/entitlements.mac.plist` only for reproduced packaging defects.

- [ ] Run `node --check app/main.js` and `node --check app/after-pack.js`.
- [ ] Freeze the engine with `uv run pyinstaller packaging/syt.spec --noconfirm`.
- [ ] Run the signed release script with the Developer ID identity and `syt-notary` profile.
- [ ] Mount the DMG, copy the app to a temporary Applications-like folder, launch it with a disposable `SYT_HOME`, ingest an export whose path contains spaces, and confirm the UI reloads.
- [ ] Verify nested signatures, hardened runtime, stapled tickets, and `spctl` acceptance for both app and DMG.

### Task 5: Publish the release and update the public handoff

**Files:**
- Modify: `docs/BUILDING-APP.md`
- Modify: `README.md`

- [ ] Replace unsigned/quarantine guidance with accurate signed/notarized installation instructions.
- [ ] Refresh committed screenshots from the final UI.
- [ ] Commit the scoped source/docs/assets changes without temporary browser or build artifacts.
- [ ] Push the current branch, update the GitHub release asset, and verify its public download metadata.
- [ ] Download the published DMG to a fresh path and repeat Gatekeeper assessment before giving the final go-ahead.

