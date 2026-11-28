# C# Native AOT manager experiment

This is a decision experiment, not the selected installer or a production TUF implementation. `core/` adapts the unmodified NuGet **Tuf 1.1.1** library to bootstrap-root replay, bounded resumable root updates, durable rollback checkpoints, explicit current-clock verified-cache replay, and independent schema-3 indefinite archival authorization. `check.py` reuses the existing Go/Rust fixture/assertion suite and adds focused adapter checks.

`app/` adds **Avalonia 12.1.1**, GUI/CLI startup and a private Qt worker smoke command. `launcher/` embeds the AOT app plus its native libraries in one public executable (a `.app` on macOS). Each run extracts into a new private temporary directory; Qt's Linux dependency paths apply only to the Qt child. No Python or installed .NET runtime participates in installed-client operations. Compilation/fixture generation still use Python and .NET SDK.

The GUI demonstrates catalog refresh; it does **not** implement the Rust installer's package resolver, payload download/validation, installation transaction, maintenance migration, uninstall, cancellation/progress UI, self-update, or signing/notarization lifecycle. Archive checks authorize the manifest; they do not verify/extract every bundle payload. Catalog retrieval validates publisher identity/scope but does not yet validate/select publisher releases. This is intentionally not a feature-parity or security-audit claim.

## Reproduce

Use a .NET SDK capable of targeting .NET 10, the platform's Native AOT toolchain (Clang/Xcode/MSVC), and `pip install -e .`. Linux distributions must be built on Ubuntu 22.04, matching the Rust ABI baseline. Runtime/ILCompiler is pinned to 10.0.15. The measured hosted builds selected preinstalled SDK 11.0.100; installing SDK 10 with setup-dotnet alone does not constrain SDK selection. Record the actual SDK and resolved package versions with the evidence. No signing keys are embedded; fixture keys are generated only under ignored build directories.

```sh
python tools/bootstrap_ifw.py
python tools/prepare_client_fixture.py --ifw .tools/ifw --work build/csharp-fixture
# Obtain the Rust manager-application.zip from successful CI run 46082370013.
python experiments/csharp-manager/build.py --source build/csharp-fixture/demo/registry-source.json --ifw .tools/ifw --work build/csharp --baseline build/rust-baseline/manager-application.zip
python experiments/csharp-manager/check.py --app "$(cat build/csharp/app-path.txt)" --work build/csharp-policy --report build/csharp-policy.json --registry-source build/csharp-fixture/demo/registry-source.json
```

Dispatch `.github/workflows/csharp-experiment.yml` for Linux x64, Windows x64, macOS Intel and macOS ARM64. It runs acceptance through the **public single application**, not only a development DLL. Linux GUI smoke uses Xvfb. The baseline is Rust commit `e40b48f7f369dceb6e46add87102590ca1f3eeb5`, run `46082370013`; its Linux artifact was compiled in the existing Ubuntu 22.04 container. Both artifact sizes exclude debug symbols and use ZIP downloads. They are unsigned development builds. Pin a new verified baseline before the old Actions artifacts expire.

`probe/` builds the policy core alone for faster iteration. Its positional protocol matches `tools/check_native_tuf.py`: `ROOT ORIGIN STATE MODE LIMIT BUDGET`. MODE `archive` interprets ROOT as the registry-source JSON and ORIGIN as the unpacked bundle. `registry SOURCE CACHE [live|cache]` fetches a registry plus its dynamically listed independent publishers. Local files and HTTP loopback are fixture transports; non-loopback HTTP and redirects are rejected. Cache replay is explicit; server/verification errors never silently select stale data.

## Trust boundaries and remaining hardening

- TUF cryptographic verification stays in the library. `ITufCache.LoadMetadata("root")` always supplies the immutable bootstrap. Only roots accepted by the library enter persisted root history. Root and metadata checkpoints are monotonic, written synchronously and atomically; failures stop the operation. Missing/tampered history fails closed. A repository-state lock serializes refreshes.
- The root-work budget applies to new downloads. Cached history is still sequentially reauthenticated on every invocation, so CPU work grows with history. The adapter throws `root-update-pending` before timestamp/catalog access; it does not reinterpret expiry or accept a fetched root as a new pin.
- Application rollback checkpoints are trusted local state. As with the Rust experiment, this does not defend against a local administrator replacing/deleting the entire application and state. Filesystem symlink/reparse-point hardening, crash/power-loss testing and root/key-rotation checkpoint reset rules need production review.
- Offline authorization has no clock dependency and never opens an online cache. Its canonical encoding matches Python ASCII JSON, including supplementary Unicode ordering and integer precision. It never grants online authorization.
- No custom TLS trust, cross-origin redirects, automatic cache fallback, root-history compaction, delegation-policy validation or persistent extracted-code cache is added. TLS/clean-machine dependency audits, credential handling, installer signing and macOS notarization are not established by hosted-runner success.
- The self-extracting AOT launcher adds startup I/O and a small second native executable privately. A final frontend could use in-process native-library extraction/loading; measure that separately before claiming this is the minimum achievable size.
