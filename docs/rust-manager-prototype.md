# Rust single-application prototype

The selected implementation is a Rust manager with a private Qt Installer Framework 4.11 engine. Windows and Linux deliver one executable; macOS delivers one `.app`. The executable implements both GUI and JSON CLI modes. Python, Cargo, and IFW tools are needed to build/test it, not to run it.

## Implemented boundary

Rust/egui owns the window from startup through verification, progress, errors and maintenance. Qt runs as a headless worker; there is no wizard handoff or separate maintenance GUI. The build embeds an exact public registry-source bootstrap and native engine archive. No signing keys or externally supplied trust override are included. Publisher discovery, profiles and component lists come from the signed bundle registry.

The user selects a browser ZIP directly. Rust snapshots it to a private temporary file, safely extracts it, authenticates the indefinite archival authorization and exact registry revision, verifies each publisher release signature and all repository hashes, validates Qt index contents, ownership and dependency compatibility, then supplies only private verified local repositories to Qt. Source media cannot be changed between verification and Qt consumption. Installed cached ZIPs undergo the same verification on every operation. Online TUF expiry is not applied to explicitly authorized archival snapshots.

Fresh installation persists the same manager application, Qt's private `.wpilib-engine` maintenance worker, and a content-addressed copy of the complete bundle in the installation. The manager automatically locates its installation when launched there. The cache reference is advanced only after Qt success and inventory validation; older bundles cannot downgrade installed packages. Failed verification or failed Qt execution does not advance the previous cache reference. An OS file lock serializes operations on an installation. Qt performs package operations and its existing rollback; the Rust application refuses to close while a worker is active.

The complete cached ZIP supports add/remove/reinstall without the original download or USB drive, at the cost of retaining a second copy of all packages. Old content-addressed ZIPs are currently retained; cache garbage collection remains future work. Temporary extraction also needs enough disk space for the complete platform bundle. There is no claim of crash-atomic installation across Qt, application persistence and state publication.

## Build and run

Use Rust 1.100.0 and the Python/IFW development prerequisites from the README. Build on the target OS/architecture. `Cargo.lock` pins the Rust dependency graph.

```sh
python tools/prepare_rust_fixture.py --ifw .tools/ifw --work build/rust-fixture
python tools/build_rust_manager.py --source build/rust-fixture/demo/registry-source.json --ifw .tools/ifw
```

Launch `build/rust-manager/download/wpilib-manager` (Linux), `WPILibManager.exe` (Windows), or `WPILib Manager.app` (macOS). Verify the bundle first, select a profile or individual packages, review/accept licenses, then apply. This prototype does not yet display upstream license documents; use approved test packages.

The same executable supports `--command verify|install|update|remove|list|check-updates`, `--root`, `--bundle`, repeated `--package`, and `--profile` for installation. Applying changes requires `--accept-licenses`. JSON goes to stdout, progress to stderr, and failures return nonzero. Omitting `--bundle` uses the installation's retained verified bundle. Omitting package IDs for `update` selects updates to installed components only. Dependencies are included for installation; removal rejects dependencies still used by installed packages.

```sh
./wpilib-manager --command install --root /path/to/wpilib --bundle platform-bundle.zip --profile tools --accept-licenses
/path/to/wpilib/wpilib-manager --command check-updates --bundle newer-platform-bundle.zip
/path/to/wpilib/wpilib-manager --command update --bundle newer-platform-bundle.zip --accept-licenses
```

## Native acceptance

`.github/workflows/rust-manager.yml` builds and runs on Windows x64, Linux x64, macOS x64, and macOS ARM64. Both synthetic multi-publisher fixtures (including RobotPy dependency/profile selection) and the pinned real Elastic/AdvantageScope release pairs are used. Each build embeds the test fixture's public authority key; these are test applications, not production-signed releases.

`tools/check_rust_manager.py` exercises:

- Native rendered startup before verification, with screenshots and startup timing; an invalid-media error stays in the application window.
- GUI-driven offline install and upgrade through the shared background operation handler.
- Installed application startup after deleting the original application and baseline ZIP.
- JSON update discovery, cache-only add/remove/reinstall, and rejection of dependency removal.
- Old-media downgrade prevention, cache corruption, ZIP traversal and invalid registry signatures.
- Installed payload hashes, symlinks, executable modes and macOS signature attributes against signed records.

GUI automation drives the same handlers as the controls and captures the renderer's framebuffer with a native window present. It is not an accessibility click test or a manual usability review. Test reports/screenshots/logs are uploaded; signing keys and generated repositories are excluded. Linux uses a network namespace and Xvfb; macOS uses `sandbox-exec` network denial; Windows uses outbound firewall rules for the exact manager and worker paths. Client PATH excludes Python, Cargo, IFW and other external runtimes. The driver still uses Python outside the application.

## Remaining gates

This is an **offline vertical prototype**, not a complete replacement for the Python client. Online tough refresh, dynamic authenticated registry renewal and durable online trust caches are still in the interoperability experiment/reference implementation. The native application does not offer an online mode yet; future integration must retain separate archival authority and verified long-root-history replay. Other unfinished work includes OS signing/notarization and nested-code layout, discovery/shortcuts/VS Code, manager self-update/recovery, installation migration, license display, cancellation/elevation, accessibility, disk-space preflight, Windows ACL hardening, Unicode filesystem-alias parity, and clean-machine/minimum-OS testing. Current Linux binaries require baseline native display/OpenGL libraries. Windows/Linux ARM64 are not tested. Refusing to close during a worker is a conservative prototype behavior, not finished cancellation UX.

The existing Python workflows remain the production-shape publication/acceptance reference. The new workflow does not silently replace offline bundle publication gates with this experimental manager.
