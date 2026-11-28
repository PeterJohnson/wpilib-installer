# C# Native AOT options for TUF

Investigation: 2026-11-28 UTC (November 27 local). **A credible pure-C# option exists: NuGet `Tuf` 1.1.1 from `mitchdenny/sigstore-dotnet`.** It merits a full manager-policy adapter experiment before deciding whether Avalonia requires a Rust verification core. This finding does not switch the selected client or claim production readiness.

The earlier comparison covered Go/Rust interoperability but did not evaluate these C# candidates. Absence from the TUF website's implementation list was not evidence that .NET lacked an implementation. GUI choice, TUF implementation and Qt engine integration remain separate decisions.

## Candidates

| Option | AOT/dependencies | Finding and disposition |
| --- | --- | --- |
| [`Tuf` 1.1.1](https://www.nuget.org/packages/Tuf/1.1.1), from [`mitchdenny/sigstore-dotnet`](https://github.com/mitchdenny/sigstore-dotnet/tree/c0f5ff9747aa9b00c45a964dca275be4066cd489/src/Tuf) | .NET 10 library; source-generated System.Text.Json; BouncyCastle.Cryptography 2.7.0 supplies Ed25519; MIT | Published stable package, successful local Native AOT compilation and scoped publisher interoperability. **Preferred pure-C# candidate for further evaluation.** Needs manager-specific trust/cache/budget adapters. |
| [`baronfel/tuf-dotnet`](https://github.com/baronfel/tuf-dotnet/tree/99ae89aac6e3986e892e8e918d8b02488746bfdf) | .NET 8 library; Serde; NSec 25.4.0 plus native libsodium; MIT | Native AOT compiles, but unmodified source fails our ordinary repository and expired-bootstrap probes. Source review finds additional root-walk/delegation gaps. **Do not select this revision without substantial corrections and conformance work.** |
| C#/Avalonia calling the existing Rust core | Native C ABI or private native worker; retain `tough` and current application policy | Preserves more tested manager behavior, at the cost of two languages, build integration and an explicit interop contract. A fallback if the pure-C# adapter does not meet acceptance; no C# bridge was implemented in this investigation. |
| New in-house C# TUF implementation / translation | Native AOT is feasible in principle | No benefit established over evaluating the existing C# library or retaining Rust. Would take responsibility for security-sensitive protocol behavior and its conformance maintenance. Not recommended as the starting point. |

The two C# projects are distinct despite similar names. NuGet package IDs are case-insensitive: `dotnet add package TUF` selects the published `Tuf` package, not a way to choose the baronfel implementation. The latter was evaluated through a pinned source reference. The downloaded `Tuf` package's `.nuspec` identifies commit `c0f5ff9747aa9b00c45a964dca275be4066cd489`, matching the inspected `v1.1.1` tag.

## Evidence obtained

Both unmodified libraries were compiled into Linux x64 Native AOT probes, rooting the whole TUF assembly for AOT analysis. The preferred probe treats warnings as errors; neither build emitted compiler, trim or AOT warnings. Builds used .NET SDK 11.0.100 targeting `net10.0`, runtime/ILCompiler 10.0.15 and Clang 22. This is a local Ubuntu 26.04 build, not proof of compatibility with the installer's older Linux baseline.

The [recorded report](evidence/csharp-tuf-2026-11-28.json) contains **18 observations**, including expected failure and policy-characterization cases. [Probe source and reproduction instructions](../experiments/tuf-clients/dotnet/README.md) are checked in separately from the installed client. Python generates test repositories through our existing publisher; the compiled probes perform their own verification without invoking Python or requiring a separately installed .NET runtime.

For `Tuf` 1.1.1, observed results include:

- Successful retrieval of the Python-generated Ed25519-signed catalog with non-ASCII content.
- Successful sequential traversal to root 1,030 with expired bootstrap/intermediate roots and threshold key rotation, using a configured larger rotation bound.
- Failure at the default 1,024-rotation cap while the intermediate root is expired, followed by successful continuation in a new process using the library's trusted local cache.
- Successful explicit raw-response cache replay with the origin removed and normal current-clock verification.
- Rejection of modified timestamp, snapshot, targets and catalog responses; all competing valid library cache copies were removed for these tests.
- Rejection of timestamp rollback across processes, validly signed expired timestamp replay, and missing/corrupt/skipped intermediate roots.

The stable source revision also has a successful upstream [Build and Release run](https://github.com/mitchdenny/sigstore-dotnet/actions/runs/37886617478), with Linux/Windows/macOS tests, an AOT compatibility job and a TUF conformance job. Its [workflow](https://github.com/mitchdenny/sigstore-dotnet/blob/c0f5ff9747aa9b00c45a964dca275be4066cd489/.github/workflows/conformance.yml) runs pinned `tuf-conformance` v2.5.0 through an AOT CLI; the checked-in TUF expected-failure file is empty. These are upstream results, not our own cross-platform acceptance or an independent security audit.

| Local probe | Executable bytes | Other runtime files in publish output |
| --- | ---: | --- |
| `Tuf` 1.1.1 | 8,784,992 (8.38 MiB) | None beside the native executable; separate debug symbols are not runtime dependencies |
| baronfel source | 7,458,992 (7.11 MiB) | `libsodium.so`, 476,568 bytes; separate symbols also emitted |

These are different library probes, not complete manager size comparisons. Neither contains Avalonia, Qt or our manager policy. `ldd` reports libc/libm/loader imports for both executables; it does not reveal all possible dynamic OS library loads. TLS, system cryptography/globalization, graphics, private Qt dependencies and minimum OS support still need full application audits. Native AOT does not by itself establish a self-contained installer on every supported OS.

## Important policy differences in the preferred candidate

**Local roots are trusted state.** The library starts from a cached root when available and checks its self-signature. It does not authenticate that cached root's full history from the application bootstrap. In a controlled characterization, replacing the cache root with another valid self-signed root allowed that other repository to be used. This describes the library's local-cache trust boundary; it is not evidence that an arbitrary network response bypasses verification. Our manager treats cached trust evidence as something to reauthenticate, so it needs a root-history adapter rather than passing arbitrary cache contents straight into this API. Never fix this by accepting a newly downloaded root as a new trust anchor.

**A work cap is not our pending protocol.** With still-current intermediate roots and unchanged role keys, a small `MaxRootRotations` can stop the walk and still return a catalog. The probe confirmed this with a five-root chain and a two-rotation cap. Our policy requires a bounded resumable operation to return `root-update-pending` before consuming a catalog until the end of the chain is established. Implement the budget in the adapter and preserve verified progress and rollback floors across failures. Increasing a numeric limit alone is not that implementation.

**Default fetching/caching needs application policy.** `ITufRepository` and `ITufCache` provide useful integration seams. The default HTTP repository buffers responses and the target API returns bytes, so apply metadata size limits, allowed URL/redirect rules, timeouts/cancellation and verified storage behavior in our adapter. Use TUF for bounded catalogs/indexes and preserve the current streaming/hash-validation path for large package archives. The default filesystem cache can disable itself when unwritable; the manager must not silently lose durable rollback state. Separate authority/publisher namespaces, transport-only fallback rules, cache revalidation and archive-vs-online state remain application responsibilities.

**Indefinite offline authorization stays separate.** Neither library supplies our `wpilib-archive-v2` registry/bundle authorization policy. A C# implementation would use the existing pinned archival authority and exactly compatible canonical bytes/signatures, independently of online expiry. Bouncy Castle provides the cryptographic primitive, but the archival policy and canonicalization must pass the existing golden/negative vectors; this probe did not port or validate them. Preserve the distinction between TUF canonical JSON and our Python-compatible archival envelope encoding.

## Why the other C# candidate is not ready for this use

At the inspected baronfel source revision, the normal publisher-format probe fails and the expired-bootstrap probe fails before making any repository request. Source review explains several blockers:

- [`TrustedMetadata.CreateFromRootData` and `UpdateRoot`](https://github.com/baronfel/tuf-dotnet/blob/99ae89aac6e3986e892e8e918d8b02488746bfdf/TUF/TrustedMetadata.cs) reject expired bootstrap/intermediate roots, preventing our old-media catch-up flow.
- [`Updater.DownloadMetadata`](https://github.com/baronfel/tuf-dotnet/blob/99ae89aac6e3986e892e8e918d8b02488746bfdf/TUF/Updater.cs) uses `root.2.json`/`snapshot.1.json`, whereas the existing repository uses standard version-prefixed `2.root.json`/`1.snapshot.json`. Its download helper does not turn HTTP 404 into the exception expected by its root loop.
- That root loop downloads repeatedly but calls `UpdateRoot` once after the loop, using only the last result; it does not verify/advance the trusted state on each fetched root.
- Non-root delegated-role verification has a `NotImplementedException` path despite broad README claims.

A successful native compilation establishes AOT feasibility, not TUF correctness. No upstream source was modified and no alternate metadata aliases were added to make this candidate pass.

## Recommendation and next decision gate

Evaluate **C#/Avalonia + `Tuf` + private Qt** as a real alternative to the current Rust frontend. Keep **Avalonia + existing Rust core + private Qt** as the comparison that minimizes trust-policy rewrites. The existing repositories, signing workflows and package catalog do not need to change for either option.

Before choosing pure C#, implement a focused adapter experiment that:

1. Replays and authenticates cached root history from the immutable bootstrap, preserves monotonic checkpoints and reports bounded catch-up as pending without accepting catalogs early.
2. Runs the full existing online/cache/archival acceptance suite, including registry refresh, independent publishers and old offline installs updating online after both root and archival-key rotations.
3. Compiles and executes as Native AOT on Windows x64, macOS x64/ARM64 and the Linux ABI baseline, with native dependency audits and clean-machine tests.
4. Measures the complete Avalonia/Qt distribution and validates one GUI/CLI application, installed maintenance, signing/self-update layout, cancellation and progress.

Those remain next-stage work. This investigation neither ports the manager nor changes the Rust/Qt implementation, and it is not a replacement for a security review of the selected dependency and adapter.
