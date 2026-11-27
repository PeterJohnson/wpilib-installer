# Rust single-application prototype

The selected implementation is a Rust manager with a private Qt Installer Framework 4.11 engine. Windows and Linux deliver one executable; macOS delivers one `.app`. The executable implements both GUI and JSON CLI modes. Python, Cargo, and IFW tools are needed to build/test it, not to run it. The [self-contained runtime policy](self-contained-runtime.md) adds embedded Linux GUI/Qt libraries, static Windows CRT linking, platform dependency audits, and clean Linux container acceptance.

## Implemented boundary

Rust/egui owns the window from startup through verification, progress, errors and maintenance. Windows uses the native Direct3D renderer (including WARP on GPU-less hosts); Linux/macOS use OpenGL. Qt runs as a headless worker; there is no wizard handoff or separate maintenance GUI. The build embeds an exact public registry-source bootstrap and native engine archive. No signing keys or externally supplied trust override are included. Publisher discovery, profiles and component lists come from either the authenticated online registry or the independently authorized bundle registry.

The user selects a browser ZIP directly. Rust snapshots it to a private temporary file, safely extracts it, authenticates the indefinite archival authorization and exact registry revision, verifies each publisher release signature and all repository hashes, validates Qt index contents, ownership and dependency compatibility, then supplies only private verified local repositories to Qt. Source media cannot be changed between verification and Qt consumption. Installed cached ZIPs undergo the same verification on every operation. Online TUF expiry is not applied to explicitly authorized archival snapshots.

Fresh installation persists the same manager application, Qt's private `.wpilib-engine` maintenance worker, and a content-addressed copy of the complete bundle in the installation. The manager automatically locates its installation when launched there. The cache reference is advanced only after Qt success and inventory validation; older bundles cannot downgrade installed packages. Failed verification or failed Qt execution does not advance the previous cache reference. An OS file lock serializes operations on an installation. The state retains package descriptions for the versions actually installed, so partial upgrades check final dependencies and file ownership against unchanged installed packages as well as offered packages. This local state relies on installation-directory permissions, like Qt's inventory; it is not an external trust input. Qt 4.11 writes a leading-dot Linux maintenance tool's data sidecar as `.dat`; the wrapper normalizes it to the filename the reader expects after successful operations. Qt performs package operations and its existing rollback; the Rust application refuses to close while a worker is active.

The complete cached ZIP supports add/remove/reinstall without the original download or USB drive, at the cost of retaining a second copy of all packages. Old content-addressed ZIPs are currently retained; cache garbage collection remains future work. Temporary extraction also needs enough disk space for the complete platform bundle. There is no claim of crash-atomic installation across Qt, application persistence and state publication.

## Build and run

Use Rust 1.100.0 (Windows also needs NASM on the build PATH) and the Python/IFW development prerequisites from the README. Build on the target OS/architecture. Linux additionally requires Docker on the build host; the manager and its embedded runtime are compiled in the Ubuntu 22.04 baseline builder. `Cargo.lock` pins the Rust dependency graph.

```sh
python tools/prepare_rust_fixture.py --ifw .tools/ifw --work build/rust-fixture
python tools/build_rust_manager.py --source build/rust-fixture/demo/registry-source.json --ifw .tools/ifw
```

Launch `build/rust-manager/download/wpilib-manager` (Linux), `WPILibManager.exe` (Windows), or `WPILib Manager.app` (macOS). Verify the bundle first, select a profile or individual packages, review/accept licenses, then apply. This prototype does not yet display upstream license documents; use approved test packages.

The same executable supports `--command verify|install|update|remove|list|check-updates`, `--root`, `--bundle`, repeated `--package`, and `--profile` for installation. Applying changes requires `--accept-licenses`. JSON goes to stdout, progress to stderr, and failures return nonzero. `--online` refreshes TUF repositories; `--cached` verifies the online cache without network access and still enforces current expiry. Both conflict with `--bundle`. Omitting source options reuses the installation’s last successfully applied source (online or retained offline bundle). Omitting package IDs for `update` selects updates to installed components only. Dependencies are included for installation; removal rejects dependencies still used by installed packages.

```sh
./wpilib-manager --command install --root /path/to/wpilib --bundle platform-bundle.zip --profile tools --accept-licenses
/path/to/wpilib/wpilib-manager --command check-updates --bundle newer-platform-bundle.zip
/path/to/wpilib/wpilib-manager --command update --bundle newer-platform-bundle.zip --accept-licenses
```

## Native acceptance

`.github/workflows/rust-manager.yml` builds and runs on Windows x64, Linux x64, macOS x64, and macOS ARM64. Both synthetic multi-publisher fixtures (including RobotPy dependency/profile selection) and the pinned real Elastic/AdvantageScope release pairs are used. Each build embeds the test fixture's public authority key; these are test applications, not production-signed releases.

`tools/check_rust_manager.py` exercises:

- Native rendered startup before verification, with screenshots and startup timing; an invalid-media error stays in the application window.
- GUI-driven offline install and upgrade through the shared background operation handler; both baseline dashboard versions are installed before upgrading.
- Installed application startup after deleting the original application and baseline ZIP.
- JSON update discovery, cache-only add/remove/reinstall, and rejection of dependency removal.
- Old-media downgrade prevention, cache corruption, ZIP traversal and invalid registry signatures.
- Installed payload hashes, symlinks, executable modes and macOS signature attributes against signed records.

GUI automation drives the same handlers as the controls and captures the renderer's framebuffer with a native window present. It is not an accessibility click test or a manual usability review. Test reports/screenshots/logs are uploaded. Successful runs also publish `rust-manager-fixture-demo-<runner>` and `rust-manager-real-demo-<runner>` artifacts containing a mode-preserving application ZIP and matching baseline/current browser bundles. Extract `manager-application.zip`, launch the application and select a matching bundle ZIP. The fixture demo contains text payloads; the real demo contains the pinned dashboards. Test signing private keys are never uploaded. These applications trust their run-specific public bootstrap and are not production-signed distributions. Linux uses a network namespace and Xvfb; macOS uses `sandbox-exec` network denial; Windows uses outbound firewall rules for the exact manager and worker paths. Client PATH excludes Python, Cargo, IFW and other external runtimes. The driver still uses Python outside the application.

## Remaining gates

This is an **online/offline vertical prototype**, not a complete replacement for the Python client. Native online TUF refresh and cache handling are now integrated; CI publication and signing remain Python build tooling. Other unfinished work includes OS signing/notarization and nested-code layout, discovery/shortcuts/VS Code, manager self-update/recovery, installation migration, license display, cancellation/elevation, accessibility, disk-space preflight, Windows ACL hardening, Unicode filesystem-alias parity, and clean-machine/minimum-OS testing. Current Linux binaries require baseline native display/OpenGL libraries. Windows/Linux ARM64 are not tested. Refusing to close during a worker is a conservative prototype behavior, not finished cancellation UX.

The existing Python workflows remain the production-shape publication/acceptance reference. The new workflow does not silently replace offline bundle publication gates with this experimental manager.

## Validated results — November 27, 2026

Code commit `53192ed` passed [Rust application CI run 46038738470](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46038738470): **136 lifecycle checks in total, plus seven Rust unit tests on each runner**, across the four native targets. Downloadable fixture and real-dashboard demo artifacts are on that run. [Machine-readable evidence](evidence/rust-application-2026-11-27.json) records application/report hashes, checks, isolation, inventories and startup measurements.

| Target | Fixture + real lifecycle checks | First process-to-startup-screenshot | Fixture application ZIP |
| --- | --- | --- | --- |
| Windows x64 | 18 + 16 | 2.30 s | 25.7 MB |
| macOS ARM64 | 18 + 16 | 1.96 s | 39.5 MB |
| macOS x64 | 18 + 16 | 6.04 s | 39.7 MB |
| Linux x64 | 18 + 16 | 3.97 s | 26.7 MB |

These timings include native window creation and rendering/encoding the third-frame screenshot, measured on hosted runners. They are not clean-desktop/quarantine benchmarks. Application ZIP sizes exclude the separate package bundles. Downloaded fixture application ZIPs were checked against the exact executable hashes in the native reports, including executable-mode preservation. The real-package checks upgrade **Elastic 2026.1.1 → 2026.1.2** and **AdvantageScope 26.0.1 → 26.0.2**, then remove/reinstall through retained media after deleting the original application and browser ZIPs. RobotPy profile checks use synthetic payloads.

The [existing installer/publication regression matrix](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46038738521) also passed on all four runners. The [existing real-dashboard matrix](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46037717085) passed on all four after the shared CI prerequisite changes. That earlier run covered the offline application. No production manager signing, notarization or full G1–G8 completion is claimed.

## Native online updates

The same application uses `tough = 0.24.0` and Rustls-backed HTTPS for the central registry and every independent publisher. The compiled bootstrap pins the original registry TUF root. A current TUF-authenticated registry supplies publisher roots, URLs, namespace allowlists, profiles and revision floors. Adding a publisher/profile needs no executable change. Registry archival-key rotation does not change the old executable’s independent offline authority pin.

```sh
/path/to/wpilib/wpilib-manager --command check-updates --online
/path/to/wpilib/wpilib-manager --command update --online --accept-licenses
/path/to/wpilib/wpilib-manager --command install --online --profile python --accept-licenses
/path/to/wpilib/wpilib-manager --command verify --cached
```

Discovery downloads TUF metadata/catalogs and the selected releases’ signed `Updates.xml` files. Compatibility selection considers up to eight retained releases per publisher with a 4,096-combination bound. Installation then fetches metadata archives and only payloads needed by the selected dependency closure. Every downloaded file has a signed length/hash, and the private copy handed to Qt is checked again. Qt sees only temporary local repositories. Removal needs metadata but does not fetch payload archives.

The sibling `.INSTALLATION.online` cache is serialized by the installation lock. It contains content-addressed package objects, raw numbered root history, immutable catalog/metadata proof generations, TUF checkpoints, and a separate last-accepted application catalog pointer. Raw root bytes are not trusted: every process authenticates their sequential chain from the compiled pin, including both old/new root thresholds. A checkpoint contains only tough-verified root/timestamp/snapshot metadata, which is re-authenticated before reuse, including after failed or interrupted refreshes. The live loader starts from that authenticated current root so replaying an *old* key rotation cannot clear today’s rollback floors. Historical proof replay ignores expiry only to establish authenticated rollback evidence; it never supplies a usable online view. All live views and cache fallback enforce the current clock and final-root expiry.

Root catch-up permits four batches of 256 remote roots per operation; exhaustion returns `root-update-pending` and retains progress for retry. The implementation has a one-million-transition resource ceiling. Missing roots, corrupt metadata, signature/expiry/rollback failures, HTTP 404 and nonavailability HTTP errors fail closed. Connection failures and HTTP 5xx may use the last fully verified view only if current-clock verification still succeeds and it does not predate newer verified progress. `--cached` never contacts an origin. Missing/corrupt payloads fail before Qt runs; cache-only operation does not imply every package was downloaded. Local state still relies on directory permissions and cannot detect an attacker restoring the entire state directory to an earlier snapshot.

A successful online upgrade preserves `bundle_sha256` and `offline_registry` for the original USB snapshot while recording the new online registry and installed package descriptions. Explicit `--bundle` verification stays archival and cannot reset online checkpoints. Online cache retention/garbage collection, cache relocation and migration remain unfinished.

`.github/workflows/rust-online.yml` runs the same native application on Windows x64, Linux x64, macOS x64 and macOS ARM64, for synthetic multi-publisher fixtures and real Elastic/AdvantageScope releases. The loopback HTTP fixture deliberately embeds an expired bootstrap, 1,030 root versions, a 2-of-3 rotation at version 257, and a rotated archival signing key. It tests resumable catch-up, registry/publisher discovery, GUI upgrades, fresh online installation, on-demand payloads, cache maintenance, real payload integrity, post-rotation rollback, incomplete-refresh floors, registry revision mutation, HTTP failure handling, actual timestamp expiry and poisoned caches. The client runs without external runtimes on PATH. Tests use a compile-time loopback-only HTTP exception; production bootstraps require HTTPS. Public TLS/proxy deployment, OS signing and clean-machine UX need separate acceptance. Test origins and keys are ephemeral and are not production publishing credentials.

## Online validation — November 27, 2026

Commit `ef768bf` passed [native online CI run 46051023760](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46051023760) on Windows x64, macOS ARM64, macOS x64 and Linux x64: **272 native checks plus 11 Rust unit tests per target**. Each target passed 35 fixture checks and 33 real-package checks. [Saved evidence](evidence/rust-online-2026-11-27.json) records check names, application/report hashes, native GUI frame timings, final inventories and the exact source commit.

The real flows install **Elastic 2026.1.1 and AdvantageScope 26.0.1 from an offline bundle**, delete the original manager download, discover their updates through independent HTTP/TUF repositories, then upgrade to **Elastic 2026.1.2 and AdvantageScope 26.0.2**. They also install fresh online, retain the offline registry pin, shut down the origin, and remove/reinstall using verified cached packages. Payload checks include file hashes, modes, symlinks and macOS signature attributes. The client runs with external runtimes removed from PATH.

The fixture checks add a new publisher/profile without rebuilding the client, require metadata-only discovery, reject invalid publisher signatures, corrupted downloads and poisoned caches, preserve rollback floors after interrupted refresh and key rotation, reject actually expired cached metadata, and recover through renewal. Catch-up from the expired bootstrap traverses 1,030 roots and requires a retry after the first operation's four-batch budget. The unit suite also writes and atomically replaces a cache file beyond the Windows 260-character path boundary.

The [existing native offline application matrix](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46048522204) and [installer/publication regression matrix](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46048522233) passed on all four targets at implementation commit `ac7dea6`. Subsequent client changes hardened online checkpoints and Windows online cache paths; the final online matrix tested those changes. CI publication/signing and Artifactory/R2 layouts remain unchanged.

To reproduce the fixture online run on a native host:

```sh
python tools/prepare_rust_online.py --ifw .tools/ifw --work build/online-fixture
python tools/build_rust_manager.py --source build/online-fixture/online-source.json --ifw .tools/ifw --assets build/online-fixture/assets --output build/online-fixture/download
python tools/check_rust_online.py --app build/online-fixture/download/wpilib-manager --fixture build/online-fixture --work build/online-fixture/native --report build/online-fixture/report.json
```

Use the platform-specific application path described above; Linux CI prefixes the test driver with `xvfb-run -a`. The driver starts/stops its loopback origin. These test applications embed ephemeral public trust roots and the loopback HTTP exception. Build with a deployed HTTPS registry-source bootstrap for an actual online repository; the older file-URL demo bootstrap is for offline testing. Public TLS/proxy deployment and clean-machine acceptance remain separate work. Downloads currently have a 120-second request deadline; resumable transfers and production download UX remain unfinished.
