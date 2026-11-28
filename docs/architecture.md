# Installer architecture

The implementation decision is **NativeAOT C#/Avalonia GUI/CLI plus a private Qt Installer Framework engine**, with Python tooling for package publication and offline assembly. The client is under development; passing the current acceptance suite does not complete the [production release gates](production-readiness.md).

## Client and installation boundary

The delivered application contains its public trust bootstrap, Qt worker, and platform runtime dependencies. The GUI and JSON CLI share the same verification, planning, and execution code. Python is not an installed-client dependency. The application persists itself and verified media/caches into the installation so that maintenance works after the original executable and USB bundle are removed.

The C# core authenticates registry and publisher metadata, checks compatibility, package ownership and payload hashes, and stages verified local repositories before launching Qt. Qt owns extraction/component operations and `components.xml`; the client does not create a competing mutable component inventory. Package scripts run with installation privileges and must be reviewed as trusted publisher code.

The public NativeAOT launcher extracts a private native application and Avalonia libraries for each invocation. Both GUI and CLI use the same application; neither launches Python or requires a .NET installation. The installed copy is the public launcher with all embedded assets, not the temporary private executable.

Linux uses host graphics, the Ubuntu 22.04 ABI baseline, and private client OpenSSL libraries. A narrowly scoped matched X11 keyboard fallback is available when the desktop lacks that bridge. Qt library/font paths are set only on its child process; the GUI does not inherit Qt's general library directory. Native Wayland is explicitly enabled; Avalonia currently describes that backend as experimental. See the [runtime contract](self-contained-runtime.md) and [client guide](client.md). Windows and macOS retain private native libraries and Qt assets.

Package removal checks retained dependency/ownership records. `uninstall` removes all installed packages using local Qt operation records without repository access; user files, the manager and caches remain available for reinstall. Complete removal of the maintenance application/caches is a separate future operation.

## Independent publication and dynamic discovery

The authority's authenticated registry describes publishers, repository URLs, component allowlists, minimum revisions, profiles, and deterministic repository precedence. Core WPILib, Elastic, AdvantageScope, and RobotPy publish independently. Other publishers can be added through a new authorized registry revision without replacing installed executables, provided the existing schema, trust policy, target, and package operations can represent them.

Repositories are ordinary HTTPS-accessible files. Qt metadata and larger combined payload archives represent installable packages, rather than individual Maven dependencies. Each publisher has its own TUF root/targets/snapshot/timestamp roles and release catalogs. Tight ABI/version coupling belongs in package compatibility constraints or a shared core release, rather than an assumed atomic release across independent publishers.

CI publishes immutable content first and advances mutable indexes last. R2/S3 uses conditional replacement; Artifactory uses staging, verification, promotion, and a serialized writer per publisher/destination. Metadata renewal does not rebuild payload archives. See [CI signing](../deployment/ci-signing.md), [TUF operations](../deployment/tuf.md), and [Artifactory](../deployment/artifactory.md).

## Package catalog migration

Develop the real catalog from pinned legacy-installer releases and Maven/upstream artifacts while building the producer-facing packaging actions in parallel. Both input paths feed the same component recipes and existing publisher handoff, so producers can adopt publication independently without changing installed package identities. This work must include offline SDK functional checks and an audit of legacy setup operations, not just archive splitting. See the [catalog development and producer CI plan](package-catalog.md).

## Online and archival trust

The native client uses `Tuf` 1.1.1 for online TUF verification; historical proof validation calls its public threshold verifier. It authenticates registry refresh and publisher catalogs, verifies cached metadata again before use, enforces expiration/rollback protection, and rejects invalid metadata rather than treating it as a transport failure. Transport failures may use still-current verified caches. Root catch-up is bounded per operation and resumable from verified progress; expired bootstrap roots and histories longer than 1,024 rotations do not bypass chain verification.

Offline bundles use a separate archival authority with explicit indefinite authorization. The signed bundle binds its registry revision/hash, exact publisher manifests and archives, and matching archival trust. No clock adjustment or network connection is needed to use matching old media. An offline bundle cannot silently teach a client to trust a new key, erase newer online cache state, or request a downgrade of an installed component.

An old offline installation can later update online using its original online root and the preserved root history. Online root/key rotation is independent of the archival authority embedded in old media. Disconnected clients cannot learn subsequent revocations; this is an explicit archival policy, not an online expiration exception.

## Browser-downloadable offline bundles

The assembler reads the registry and selects the latest compatible publisher revisions for one OS/architecture. It includes that complete current package set, its authenticated registry snapshot and trust evidence, and a signed binding. It excludes other platforms and unrelated historical versions. The user downloads the platform ZIP and matching installer through a normal browser and can carry both on USB.

Profiles such as C++, Java, Python, and tools are registry data; individual components can be selected within the client. RobotPy will include a standalone Python installation and its required packages as normal versioned components. Current RobotPy/SDK entries are test markers: production recipes and dependency closure remain release work.

Offline publication validates the exact candidate and upgrade baseline on native platforms before promoting the download page/index. Failed assembly, validation, upload or promotion preserves the last successful bundle. Producer release notifications and reconciliation can refresh bundles without blocking independent online releases. See [offline publication](../deployment/offline-bundles.md) for storage layouts and signing requirements.

## Repository responsibilities

- `client/`: installed application, TUF verification, caches, planning, UI/CLI, and worker execution.
- `tooling/installer_tooling/`: publisher/authority operations, assembly, recipes, and native publication gates.
- `installer_tooling.qt_engine`: build the private worker and inspect Qt inventory.
- `installer_tooling.reference_client`: Python acceptance adapter for tests and publication gates; not shipped to end users.
- `tools/` and `.github/workflows/`: repeatable builds and acceptance entry points, including fresh Desktop VMs and headless containers.
- `packaging/` and `deployment/`: reviewed upstream inputs and deployment contracts.

Package IDs, wire schemas, `.wpilib-manager.json`, cache locations, and Qt inventory are compatibility contracts. See the [development guide](../CONTRIBUTING.md) for build and test commands.

## Remaining production decisions

Installation layout/migration, interruption recovery, user/elevation policy, complete profile UX, manager self-update, signed delivery, VS Code discovery/launch integration, real SDK/RobotPy packaging, and the wider clean-machine/hardware matrix remain explicit work. The [readiness plan](production-readiness.md) is the current checklist.
