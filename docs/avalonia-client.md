# C#/Avalonia installation and maintenance port

The selected client is now `client-dotnet/`: a single NativeAOT C#/Avalonia application with an embedded private Qt engine. `tools/build_client.py` builds it for Windows x64, macOS Intel/ARM64 and Linux x64. The former Rust client, Cargo configuration, builders and Rust TUF probe have been retired; their source remains available in Git history, and historical evidence is preserved.

The operation layer ports offline archive authorization, publisher manifests, dependency closure, profiles, package ownership, versions/downgrade rejection, Qt inventory, installation locks and retained maintenance state. Online operations authenticate registry renewal and each publisher repository, stage only requested payload archives, and reverify caches before use. Metadata failures never become transport fallback. Partial root and timestamp progress survives failed refreshes; every root transition is verified from the original bootstrap, with a 1,024-new-root per-operation budget and no fixed lifetime rotation limit.

Offline bundles retain their authenticated registry revision and an independent indefinite archival authorization. Using old media cannot reset online floors. An offline installation can switch to online updates and continue operating after its original launcher and ZIP have been removed.

`remove` removes selected packages while enforcing installed dependencies. `uninstall` removes all packages from local Qt records with no repository access, preserves user-created files, the maintenance application and verified caches, and supports reinstall. Explicit confirmation is required. Full removal of the manager/caches is not yet implemented.

The UI supports source/folder/media selection, registry-defined profiles, individual package selection, install/update/remove/uninstall, progress output and an uninstall confirmation. Automated GUI checks invoke the same operation handler and save rendered startup/final screenshots. CLI output remains JSON. The launcher forwards stdout/stderr and retains the public single application, never the extracted private GUI binary.

## Native acceptance

The lifecycle port has native evidence separate from the earlier trust-only experiment and Rust reference. The [machine-readable report](evidence/avalonia-client-2026-11-28.json) binds the source revisions, workflow jobs, executable hashes, exact checks and upstream release hashes. These are development artifacts with disposable trust keys.

| Target | Fixture offline | Fixture online | Trust/archival | Real offline | Real online |
| --- | ---: | ---: | ---: | ---: | ---: |
| Windows x64 | 22 | 37 | 39 | 20 | 35 |
| macOS Intel | 22 | 37 | 39 | 20 | 35 |
| macOS Apple Silicon | 22 | 37 | 39 | 20 | 35 |
| Linux x64 | 22 | 37 | 39 | 20 | 35 |

The real suites use Elastic 2026.1.1 → 2026.1.2 and AdvantageScope 26.0.1 → 26.0.2. They verify payload bytes, modes, symlinks, obsolete-file removal and Qt inventory; they do not launch the dashboard applications. Offline suites deny network access, delete the original application and media, then exercise retained maintenance, uninstall and reinstall. Online suites install baseline media, authenticate updated catalogs, download requested archives, upgrade, and test maintenance/uninstall with the origin unavailable.

Native Windows/macOS acceptance uses hosted CI machines with external runtimes removed from the application PATH. This is not a claim of pristine Windows/macOS installation coverage. Linux additionally passes 44 checks across unmodified networkless Ubuntu 22.04, 24.04, 26.04 and Debian 12 containers.

The [native build jobs](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46110884362) test client revision `a22563d`. [Real online acceptance](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46108953843) and [real offline acceptance](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46108955677) test the same core operation/trust code at `23e0470`; later changes adjust test-only native-window diagnostics and Windows firewall cleanup. The successful [Windows offline rerun](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46109620735) includes the DNS-cache cleanup needed after restoring network access for artifact upload.

Fresh stock Ubuntu Desktop 24.04.4 and 26.04.1 each passed 22 fixture checks and 26 real-package checks, covering both native Wayland and X11. The [fixture Desktop run](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46111623791) and [real Desktop run](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46111626207) use the original `a22563d` executables with controller revision `19068b0`. The controller change accounts for the four added uninstall/reinstall cases. Superseded dependent Desktop jobs in the original build runs were cancelled; all four native client jobs and the real-package build job had already passed.

The VMs install from hash-pinned Desktop ISOs onto new disks, have no network interface and receive no added guest packages. Acceptance confirms native windows, screenshots, OS graphics libraries and isolated Qt libraries, then exercises maintenance after deleting the original installer and bundles. The Wayland diagnostic checks the native render surface: Avalonia intentionally exposes no generic platform handle there, and keyboard activation is not required. The [publisher-tooling matrix](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46110884379) also passed on all four targets.

The selected `client.yml` gate builds NativeAOT with warnings-as-errors on all four targets and runs offline startup/install/upgrade, cache maintenance after deleting original media, dependency/downgrade/tampering rejection, all-package uninstall and reinstall. Online coverage includes dynamic publishers, 1,030-root history, signature/rollback/cache attacks, expiry/renewal, on-demand payloads, offline-to-online upgrade, unavailable-network fallback, and uninstall with the server stopped. End-user operations run with external runtimes absent from PATH.

Linux also retains networkless headless Ubuntu/Debian checks and clean Ubuntu Desktop Wayland/X11 checks. Windows offline acceptance temporarily blocks all outbound traffic on the disposable runner, including the randomly extracted native child, then removes the rule in `finally`. macOS uses sandbox-exec network denial; Linux uses a network namespace.

## Measured distribution sizes

The `a22563d` fixture builds include the NativeAOT launcher, GUI/CLI, private Qt worker and runtime libraries, and public trust bootstrap. Package payloads are separate downloads or offline bundles.

| Target | Executable | Browser ZIP |
| --- | ---: | ---: |
| Windows x64 | 36.4 MiB | 35.2 MiB |
| macOS Intel | 53.4 MiB | 52.0 MiB |
| macOS Apple Silicon | 53.1 MiB | 51.7 MiB |
| Linux x64 | 48.5 MiB | 47.0 MiB |

The macOS executable measurement is the binary inside the `.app`; the ZIP includes the app wrapper. Exact bytes and SHA-256 values are in the evidence report. Builds are unsigned/unnotarized and are not claimed to be bit-for-bit reproducible. Linux uses desktop graphics drivers and keeps Qt dependencies private; no Mesa/LLVM renderer or external .NET runtime is required.

## Remaining production work

This is a functional development client, not a production release. Real SDK/RobotPy recipes, finished license presentation and UX/accessibility, interrupted-operation recovery, complete uninstall/cache cleanup, desktop OS integration, client self-update, signing/notarization, supply-chain review and pristine Windows/macOS acceptance remain open in [the readiness plan](production-readiness.md). Native Wayland is still experimental upstream. Historical test artifacts use disposable trust keys and must not become production installers.
