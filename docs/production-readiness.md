# Production-readiness plan

The selected implementation is the Rust client in `client/` with a private Qt installation engine. Python is publishing/build/acceptance tooling. This repository has moved beyond choosing an implementation, but **there is no production release readiness claim**. Test artifacts embed disposable public trust and must not be distributed as the production installer.

## Established capabilities

- One GUI/CLI application, with retained private worker and verified caches for maintenance after original downloads are removed.
- Fresh online/offline installation, package-level updates, dynamic authenticated publisher registries, and independent publisher repositories.
- Rust TUF verification with authenticated cache fallback, rollback checks, and root catch-up beyond 1,024 rotations. Offline archival authority is independent and permits matching bundles indefinitely.
- Latest-compatible per-platform offline bundles and browser pages, with last-success preservation on assembly/publication failure.
- R2/S3 conditional publication and Artifactory staging/promotion with serialized publisher index updates.
- Cross-platform native fixture and real dashboard installation checks; clean Ubuntu Desktop graphics/worker isolation and headless Linux acceptance. The [runtime policy](self-contained-runtime.md) and [evidence index](README.md) distinguish exact coverage and limitations.

## Validation of the production-development layout

The real dashboard Desktop gate passed before the restructure ([run 46079025954](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46079025954)). The reorganized source at `e40b48f` then passed all three dispatched workflows:

- [Installer client](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46082370013): 212 acceptance checks and 44 Rust unit tests across four native targets, four headless Linux images, and two clean Ubuntu Desktop installations.
- [Publisher tooling](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46082373513): 110 tests per target, with 109 passing on Linux, 108 on Windows, and all 110 on each Mac architecture. Skips are macOS-specific payload tests on other hosts. All four native offline publication gates passed.
- [Real dashboard Desktop](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46082377041): 44 checks across Ubuntu 24.04.4 and 26.04.1, exercising both Wayland and X11, real Elastic/AdvantageScope upgrades, and removal/reinstallation from retained caches after original installers and bundle media were deleted.

The [layout report](evidence/production-layout-2026-11-27.json) and [real-package report](evidence/real-desktop-production-layout-2026-11-27.json) preserve exact source, application, package, and test identities. These runs validate the installer and installed payloads; they do not launch the dashboard applications. The release gates below remain open.

## Release gates

All unchecked gates below remain required before calling an installer production-ready. A successful fixture build is necessary evidence, not release approval.

- [ ] **Package catalog:** package real WPILib C++/Java toolchains, libraries, VS Code integration, and tools; implement RobotPy's standalone Python and wheel/runtime closure. Define supported versions, dependencies, owned paths, licenses, and compatibility across releases. Develop pinned legacy-release/Maven importers alongside producer-output adapters and packaging actions, sharing recipes and equivalence/offline-build tests; see the [catalog migration plan](package-catalog.md). Current RobotPy/SDK fixtures are markers only.
- [ ] **Installation behavior:** settle seasonal/per-user/system-wide roots, upgrade migration from the existing installer, launchers/shortcuts, file associations, permissions/elevation, and applications running during updates. Test interrupted extraction, failed operations, rollback/recovery, disk-full behavior, and Windows reboot/replacement scenarios.
- [ ] **Client UX:** finish profile/component selection, license review, understandable progress/errors, cancellation, accessibility/localization, disk-space checks, proxy/TLS configuration, retry/resume, and cache cleanup. The current GUI and download behavior are development interfaces.
- [ ] **Trust and deployment:** provision reviewed HTTPS registries and publisher allowlists; conduct root/archival key ceremonies, define custody/thresholds and recovery drills, configure protected CI environments and least-privilege secrets, and validate real Artifactory/R2 publication end to end. Keep fixture trust and loopback exceptions out of release bootstraps.
- [ ] **Client delivery:** implement reviewed release builds, Windows signing, macOS signing/notarization/quarantine validation, authenticated manager self-update and recovery, provenance/SBOM, dependency update policy, and complete Qt/upstream source/relinking/license obligations. No production client release workflow is enabled by this restructuring.
- [ ] **Acceptance matrix:** run pristine supported Windows/macOS installations, macOS 13, representative Intel/AMD/NVIDIA Linux GPUs, international keyboards/compose input, supported filesystem/USB behavior, and installation interruption scenarios. Linux/Windows ARM remain blocked until native tooling and acceptance runners exist. Virtual graphics do not establish physical-GPU coverage.
- [ ] **Integrations and operations:** implement VS Code update discovery/launch behavior, supported diagnostics/log collection and support procedures, release retention/revocation communication, and monitoring of metadata renewal and last-successful offline bundle age.
- [ ] **Release governance:** configure required CI status checks for the current workflow names, define release owners and rollback decisions, and bind approval to the exact signed client, package set, trust roots, and acceptance reports. Publisher publication gates must continue to verify the exact candidate and upgrade baseline.

## Next implementation sequence

1. Build real core packages from a pinned last legacy release and/or Maven artifacts while developing equivalent producer CI recipes/actions in parallel; add RobotPy from its own runtime/wheel sources. Settle installation/layout contracts and validate offline development functionality as described in the [catalog plan](package-catalog.md).
2. Harden lifecycle recovery and complete normal-user GUI flows and VS Code integration.
3. Provision deployment trust/endpoints and implement signed client release/self-update delivery.
4. Complete the expanded clean-machine/hardware matrix and release approval process.

Repository organization does not change protocol or trust policy. Historical alternatives and prototype evidence remain available under [history](history/); active development instructions are in [CONTRIBUTING.md](../CONTRIBUTING.md).
