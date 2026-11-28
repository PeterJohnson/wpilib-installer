# Installed C#/Avalonia application

The selected client is one NativeAOT executable (one `.app` on macOS), containing the GUI/JSON CLI, public trust bootstrap, native libraries and private Qt installation engine. End users do not install .NET or Python. Build from the repository root with `tools/build_client.py`; see [CONTRIBUTING](../CONTRIBUTING.md).

- `launcher/`: public application; extracts compiled private assets into a fresh per-run directory, forwards arguments/results, and identifies the retained public executable.
- `app/`: Avalonia UI and JSON CLI entry point; both invoke the same background operation layer. Native Wayland is selected when `WAYLAND_DISPLAY` exists; otherwise platform detection selects X11, Cocoa or Win32.
- `core/Installation.cs`: package operations, installed state, locks, worker execution and retained maintenance application.
- `core/Bundle.cs`, `Files.cs`: bundle verification, safe extraction, inventory, ownership and dependency planning.
- `core/OnlinePackages.cs`: authenticated registry/publisher selection, compatibility and bounded payload downloads.
- `core/CatalogStore.cs`, `Online.cs`, `TrustHistory.cs`: current-clock TUF verification, signed rollback checkpoints, historical proof authentication, resumable roots and verified caches.
- `core/Archive.cs`: independent indefinite archival authority; never bypasses online expiry.

`Tuf` 1.1.1 verifies online metadata and root thresholds. Historical cache proofs are authenticated from the original pin through the library's public threshold verifier, solely to establish rollback history; obtaining a usable view still requires a new current-clock `TufClient` verification. No cached root becomes an independent trust anchor.

Commands: `verify`, `check-updates`, `list`, `install`, `update`, `remove`, `uninstall`; source selection: `--bundle ZIP`, `--online`, `--cached`, or the last installed source. Use repeated `--package ID` or an install `--profile NAME`. Mutations require `--accept-licenses`. `uninstall` removes every package from installed records without contacting repositories, preserving user files, the manager and caches; this supports later reinstall. A complete manager/cache removal command remains future work.

Running a separately downloaded manager with `--root EXISTING_ROOT` does not automatically replace the retained manager until a successful modifying operation. The bootstrap's scope/archival key must match; arbitrary test binaries are not interchangeable.

See [architecture](../docs/architecture.md), [acceptance and limitations](../docs/client.md), and [release gates](../docs/production-readiness.md).
