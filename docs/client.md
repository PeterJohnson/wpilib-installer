# Installer client

`client/` contains a single NativeAOT C#/Avalonia application with an embedded private Qt Installer Framework engine. `tools/build_client.py` builds Windows x64, macOS Intel/ARM64 and Linux x64 distributions. End users do not install .NET or Python. GUI and JSON CLI operations use the same verification, planning and execution code.

## Installation and maintenance

The client verifies archival authorization, publisher manifests, payload hashes, dependency closure, profiles, package ownership and versions before staging a local repository for Qt. Qt owns component operations and installed inventory. Installation locks and retained state bind maintenance to the installation's trust and scope.

Online operations authenticate the registry and each publisher repository, download only requested archives, and reverify caches before use. Metadata verification failures never become network fallback. Verified root and timestamp progress survives failed refreshes. Every root transition is verified from the original bootstrap, with a 1,024-new-root per-operation budget and no fixed lifetime rotation limit.

Offline bundles preserve their registry revision and independent indefinite archival authorization. Old media cannot reset online rollback floors. An offline installation can later use online updates. The client retains its public application and verified media/caches so maintenance works after original downloads and USB media are removed.

`remove` removes selected packages while enforcing installed dependencies. `uninstall` removes all packages from local Qt records without repository access, preserving user-created files, the maintenance application and caches for reinstall. Explicit confirmation is required. Complete manager/cache removal remains release work.

The GUI supports source/folder/media selection, registry-defined profiles, individual packages, install/update/remove/uninstall and progress output. The public launcher forwards CLI output and retains the complete public application for maintenance. See the [command guide](../client/README.md) and [build instructions](../CONTRIBUTING.md).

## Acceptance workflows

- `client.yml` builds all four NativeAOT targets with warnings as errors and runs offline lifecycle, online/cache, TUF and archival checks. Linux adds networkless headless containers and fresh Ubuntu Desktop VM checks.
- `client-offline.yml` and `client-online.yml` run extended fixture and real Elastic/AdvantageScope lifecycle suites.
- `desktop-real.yml` packages pinned dashboard upgrades and tests fresh Ubuntu Desktop installations under native Wayland and X11.
- `tooling.yml` and `dashboard-releases.yml` verify publication tooling and native exact-package release gates.

Trust checks are maintained in `tools/client_trust_checks.py` and `tools/client_policy_checks.py`, invoked through `tools/check_client_trust.py`. They exercise signed fixtures, verified-cache failures, rollback and expiry, resumable long root histories, dynamic publishers, independent archival authority and canonical encoding. Online fixture generation reuses the same signed-root generator.

Offline tests deny network access, delete the original application/media and verify retained maintenance, uninstall and reinstall. Online tests include offline-to-online upgrades, on-demand payloads, unavailable-origin fallback and server-off uninstall. Real-package tests verify payload bytes, modes, symlinks, obsolete-file removal and Qt inventory; they do not launch dashboard applications.

CI uploads reports, hashes, logs and rendered screenshots as build artifacts. Windows/macOS hosted-runner checks use PATH isolation and dependency audits; pristine desktop validation remains a release gate. Linux VM/container conditions and graphics-library isolation are described in the [runtime contract](self-contained-runtime.md).

## Release requirements

This is a development client. Real SDK/RobotPy recipes, license presentation and UX/accessibility, interrupted-operation recovery, complete uninstall/cache cleanup, OS integration, client self-update, signing/notarization, supply-chain review and expanded clean-machine testing remain in the [readiness plan](production-readiness.md). Native Wayland requires further production evaluation. Test artifacts embed disposable trust keys and must not be distributed as production installers.
