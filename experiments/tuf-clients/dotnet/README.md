# C# Native AOT TUF investigation

These are isolated evaluation programs, not installer code. The current Rust client and publisher tooling do not reference them.

- `sigstore/` uses the published **NuGet `Tuf` 1.1.1**, from `mitchdenny/sigstore-dotnet` tag `v1.1.1` (`c0f5ff9747aa9b00c45a964dca275be4066cd489`). Its transitive cryptography package is BouncyCastle.Cryptography 2.7.0.
- `baronfel/` references **baronfel/tuf-dotnet** at `99ae89aac6e3986e892e8e918d8b02488746bfdf`, built from source. NuGet names are case-insensitive: `dotnet add package TUF` is not a way to select this second implementation. Use its source reference explicitly.

The probes root the complete TUF assembly for Native AOT analysis. `sigstore` additionally treats warnings as errors. Both observed builds had no compiler/trim/AOT warnings. Libraries were not patched to make the tests pass. The local test environment used SDK 11.0.100 targeting `net10.0`, runtime/ILCompiler 10.0.15, Linux x64, and Clang 22. Lockfiles record this resolution; other host RIDs require deliberate lock regeneration and their own native tests.

## Reproduce

From the repository root, with publishing/test Python dependencies installed, .NET SDK 11.0.100 and Native AOT prerequisites:

```sh
mkdir -p build/csharp-tuf
git clone https://github.com/baronfel/tuf-dotnet.git build/csharp-tuf/baronfel-source
git -C build/csharp-tuf/baronfel-source checkout --detach 99ae89aac6e3986e892e8e918d8b02488746bfdf
dotnet publish experiments/tuf-clients/dotnet/sigstore/Probe.csproj -c Release -r linux-x64 -p:RestoreLockedMode=true -o build/csharp-tuf/sigstore
dotnet publish experiments/tuf-clients/dotnet/baronfel/Probe.csproj -c Release -r linux-x64 -p:RestoreLockedMode=true -p:TufSourceRoot="$PWD/build/csharp-tuf/baronfel-source" -o build/csharp-tuf/baronfel
python experiments/tuf-clients/dotnet/check.py --sigstore build/csharp-tuf/sigstore/Probe --baronfel build/csharp-tuf/baronfel/Probe --work build/csharp-tuf/run-1 --report build/csharp-tuf/report.json
```

Use a fresh work directory. The harness creates throwaway signing keys under that directory; never commit or upload it. Only the summary report and public source/dependency identities belong in evidence. No live repositories are contacted by the probes: `https://fixture.invalid/` is a synthetic namespace backed by local files. The Python driver generates repositories using the existing publisher; the native binaries neither launch a dotnet host nor call Python to verify them.

## What the observations mean

The first fourteen observations exercise normal catalog interoperability, 1,030-root catch-up with expired bootstrap/intermediates and threshold key rotation, cap failure/restart, raw-response cache replay at the current clock, tampered metadata/target rejection, timestamp rollback across processes, expiry and missing/corrupt/skipped intermediate roots. Cache replay is an explicit probe transport, not a claim that the library's HTTP adapter automatically falls back offline. When testing tampered raw responses the library cache is removed to ensure no valid second copy can legitimately recover the data.

Two more observations deliberately characterize policy gaps in `Tuf`:

- A valid self-signed replacement root in the library's local trusted cache is accepted without proving its history from the supplied bootstrap. This is the library's trusted-local-state boundary, not evidence that an unauthenticated network response bypasses TUF. Our manager's verified-cache contract needs an additional authenticated root-history adapter.
- A short rotation limit with still-valid intermediate roots can return a catalog before reaching the repository's newest root. Our stricter pending/budget policy must interrupt before that catalog can be used. Simply changing `MaxRootRotations` is insufficient for bounded, resumable manager policy.

The last two observations confirm the other candidate's current failures with the unmodified publisher format and expired bootstrap. The harness expects those failures because this is an investigation report, not an assertion that both libraries are acceptable.

No native archival-authority verifier, authority/publisher application policy, complete manager port, safe production HTTP transport, GUI, clean-machine packaging, Windows/macOS execution, or independent security audit is established here. The test binary size is not an Avalonia/Qt installer size.

See the [evaluation](../../../docs/csharp-tuf-evaluation.md) and [recorded observations](../../../docs/evidence/csharp-tuf-2026-11-28.json).
