# C#/Avalonia installation and maintenance port

The selected client is now `client-dotnet/`: a single NativeAOT C#/Avalonia application with an embedded private Qt engine. `tools/build_client.py` builds it for Windows x64, macOS Intel/ARM64 and Linux x64. Rust remains an explicit reference build in `client/` and `tools/build_rust_client.py`.

The operation layer ports offline archive authorization, publisher manifests, dependency closure, profiles, package ownership, versions/downgrade rejection, Qt inventory, installation locks and retained maintenance state. Online operations authenticate registry renewal and each publisher repository, stage only requested payload archives, and reverify caches before use. Metadata failures never become transport fallback. Partial root and timestamp progress survives failed refreshes; every root transition is verified from the original bootstrap, with a 1,024-new-root per-operation budget and no fixed lifetime rotation limit.

Offline bundles retain their authenticated registry revision and an independent indefinite archival authorization. Using old media cannot reset online floors. An offline installation can switch to online updates and continue operating after its original launcher and ZIP have been removed.

`remove` removes selected packages while enforcing installed dependencies. `uninstall` removes all packages from local Qt records with no repository access, preserves user-created files, the maintenance application and verified caches, and supports reinstall. Explicit confirmation is required. Full removal of the manager/caches is not yet implemented.

The UI supports source/folder/media selection, registry-defined profiles, individual package selection, install/update/remove/uninstall, progress output and an uninstall confirmation. Automated GUI checks invoke the same operation handler and save rendered startup/final screenshots. CLI output remains JSON. The launcher forwards stdout/stderr and retains the public single application, never the extracted private GUI binary.

## Native acceptance

Validation is in progress on the review branch. Final run IDs, measured sizes, counts and hashes will be recorded here after completion. No passing claim is inferred from the earlier C# trust-only experiment or Rust evidence.

The selected `client.yml` gate builds NativeAOT with warnings-as-errors on all four targets and runs offline startup/install/upgrade, cache maintenance after deleting original media, dependency/downgrade/tampering rejection, all-package uninstall and reinstall. Online coverage includes dynamic publishers, 1,030-root history, signature/rollback/cache attacks, expiry/renewal, on-demand payloads, offline-to-online upgrade, unavailable-network fallback, and uninstall with the server stopped. End-user operations run with external runtimes absent from PATH.

Linux also retains networkless headless Ubuntu/Debian checks and clean Ubuntu Desktop Wayland/X11 checks. Windows offline acceptance temporarily blocks all outbound traffic on the disposable runner, including the randomly extracted native child, then removes the rule in `finally`. macOS uses sandbox-exec network denial; Linux uses a network namespace.

## Remaining production work

This is a functional development client, not a production release. Real SDK/RobotPy recipes, finished license presentation and UX/accessibility, interrupted-operation recovery, complete uninstall/cache cleanup, desktop OS integration, client self-update, signing/notarization, supply-chain review and pristine Windows/macOS acceptance remain open in [the readiness plan](production-readiness.md). Native Wayland is still experimental upstream. Historical test artifacts use disposable trust keys and must not become production installers.
