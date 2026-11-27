# Installed application

This Rust crate is the selected end-user client: one GUI/JSON-CLI application, native TUF verification, verified online/offline caches, and a private Qt worker. It is under active development; see the [production-readiness plan](../docs/production-readiness.md).

Build through `tools/build_client.py` from the repository root. `build.rs` requires an explicitly supplied `WPILIB_ASSET_DIR` containing the public bootstrap and private engine/runtime archives. No signing keys are embedded. Linux builds use the Ubuntu 22.04 baseline container. The [development guide](../CONTRIBUTING.md) contains commands and platform prerequisites.

Modules separate trust (`trust.rs`), authenticated online state (`online.rs`), offline bundle verification/planning (`bundle.rs`), Qt execution and installed state (`core.rs`), process-specific dependencies (`runtime.rs`), and the GUI (`ui.rs`). The CLI and GUI share the same operations.

Keep existing package, cache, and installed-state formats compatible unless a separately reviewed migration changes them. Layout changes in this repository do not migrate users' installations. Runtime baseline and acceptance results are documented in the [runtime policy](../docs/self-contained-runtime.md).
