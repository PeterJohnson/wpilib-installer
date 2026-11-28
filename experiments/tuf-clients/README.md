# Native TUF interoperability probes

These are evaluation programs, not supported installer entry points. The retained Go probe uses unmodified `go-tuf/v2` 2.5.0 against Python-published repositories. The [C# probes](dotnet/README.md) evaluate .NET libraries separately. The Rust `tough` probe and Cargo infrastructure were retired after the C#/Avalonia migration; their source is in Git history and their [measured results](../../docs/evidence/native-tuf-2026-11-27.json) remain historical evidence.

`tools/check_native_tuf.py` also supplies the shared fixtures and assertions used by the selected C# client's trust gate. It must remain available independently of the experimental Go executable.

## Build and run the Go probe

Install the repository's Python development dependencies and Go 1.27.4. Python generates fixtures and drives the native process; the compiled probe does not invoke Python.

```sh
mkdir -p build/native-tuf
cd experiments/tuf-clients/go
CGO_ENABLED=0 go build -mod=readonly -buildvcs=false -trimpath -o ../../../build/native-tuf/go-probe .
cd ../../..
python tools/check_native_tuf.py --go build/native-tuf/go-probe --work build/native-tuf/run-1 --report build/native-tuf/report.json
```

Use a new work directory for each run. Windows requires setting `CGO_ENABLED=0` in the environment and using `.exe` paths. The saved Go evaluation covers Linux x64 only. Optional `--registry-source /absolute/path/registry-source.json` compares still-current local publisher repositories with the Python client; it does not modify them or bypass expiry.

## Scope

The probe uses directory-backed transports for `https://fixture.invalid/`; it makes no network request. Live mode reads local repository bytes, while cache mode replays saved responses with the origin unavailable. Every replay starts at the original bootstrap and uses normal current-clock expiry checks. Bounded root catch-up is resumable, with no catalog returned while root updates are pending. The library's own metadata store retains rollback state.

The separate archive path verifies Ed25519 envelopes, canonical JSON, the archival authority, registry binding, publisher provenance and indefinite authorization against the Python oracle. It verifies authorization, not full payload extraction. Vectors cover Unicode, control characters, booleans/null and integers larger than 2^53; floating-point values are rejected.

These probes do not cover production HTTP, redirects/proxies, download limits, crash recovery, GUI or application packaging. Fixture private keys stay under ignored build directories and must not be uploaded as evidence. See the [historical comparison](../../docs/history/manager-distribution-evaluation.md#native-library-interoperability-results) for the original two-language results and limitations.
