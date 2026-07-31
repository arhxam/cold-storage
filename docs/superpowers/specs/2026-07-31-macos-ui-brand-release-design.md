# macOS UI, Brand, and Distribution Design

## Goal

Finish Save Your Shit as a polished, self-contained macOS app that a person can download, drag to Applications, open without Gatekeeper workarounds, and use without a terminal.

## Product surface

The archive keeps the existing local-only architecture and three-pane information model:

- A narrow navigation rail for the archive overview and platforms.
- A conversation or collection list for the selected platform.
- A focused content pane for dashboard metrics, messages, followers, following, posts, saved items, empty states, loading states, and errors.

No archive, connector, encryption, or ingest semantics change in this pass. The work is visual, interaction, packaging, and release hardening around the already-tested engine.

## Visual system

The app uses near-black neutral surfaces, restrained borders, compact native typography, and a single cobalt-blue brand accent. It must feel like a serious macOS utility, not a web dashboard template. Platform colors remain limited to the small platform identity tiles; status colors remain semantic.

The supplied rough-logo intent is normalized into a vector-friendly blue shield/archive mark. The same geometry is used in:

- An editable SVG source.
- App-window branding.
- macOS `.icns` app and Dock icon.
- PNG exports at common sizes.
- README and release presentation.

The mark stays legible at 16 px, contains no text, and works on light and dark backgrounds.

## Interaction and states

Navigation, conversation selection, collection selection, search filtering, and dashboard cards must be keyboard-focusable and use real buttons. User-derived text is escaped before insertion. Network transitions use skeletons, errors are visible, and empty results have explicit guidance. Motion is limited to short opacity/position transitions and is disabled by `prefers-reduced-motion`.

The layout must remain usable at the packaged window's minimum width, a normal laptop window, and a wide desktop window. Narrow mode collapses the platform rail to icons while preserving the list and content panes.

## macOS distribution

The Electron shell bundles the frozen Python engine and uses a dynamic localhost port. Every nested Mach-O is signed inside-out with the Developer ID Application certificate, then Electron signs the outer app with hardened runtime entitlements. The DMG is signed, submitted to Apple's notary service, stapled, and assessed with Gatekeeper.

The notarized DMG is the primary GitHub download. The CLI remains available for contributors but is no longer the primary setup path in the README.

## Acceptance criteria

- Dashboard, messages, search, followers/following, non-person collections, empty archive, empty search, and loading/error structures render cleanly.
- There are no decorative emoji or external resources in the local UI.
- The blue SVG and generated macOS icon assets are checked into the repository and referenced by the app and README.
- Python tests, lint, Electron syntax checks, packaged-app smoke tests, code-sign verification, notarization/stapling validation, and Gatekeeper assessment all pass.
- A fresh downloaded DMG opens normally without Terminal commands or a quarantine bypass.

