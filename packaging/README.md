# Real dashboard release recipes

The dashboard adapter packages the official **WPILib distributions** of
[Elastic](https://github.com/Gold872/elastic_dashboard/releases) and
[AdvantageScope](https://github.com/Mechanical-Advantage/AdvantageScope/releases).
Each remains an independent publisher and Qt component. The recipes preserve the
complete portable application, bundled runtimes/resources, and upstream notices;
no upstream installer is run during packaging or installation.

`upstream-lock.json` pins exact release asset URLs, sizes, SHA-256 digests, and
project licenses. The checked-in upgrade pairs are Elastic **2026.1.1 → 2026.1.2**
and AdvantageScope **26.0.1 → 26.0.2**. Assets and checksums were retrieved from the
official GitHub release APIs. No floating `latest` URL is used. Downloads and cache
hits are verified against the lock before extraction. Updating the lock is a
reviewed source change; hashes alone do not independently establish publisher
identity.

| Target | Elastic asset | AdvantageScope asset |
| --- | --- | --- |
| Windows x64 | `Elastic-WPILib-Windows.zip` | `advantagescope-wpilib-win-x64.zip` |
| Linux x64 | `Elastic-WPILib-Linux.zip` | `advantagescope-wpilib-linux-x64.zip` |
| macOS x64 | `Elastic-WPILib-macOS.tar.gz` (universal) | `advantagescope-wpilib-mac-x64.tar.gz` |
| macOS ARM64 | `Elastic-WPILib-macOS.tar.gz` (universal) | `advantagescope-wpilib-mac-arm64.tar.gz` |

## Produce a publisher workflow input

From the repository root with the project installed:

```sh
python -m installer_prototype.dashboard_packages --publisher elastic --target linux-x64 --release v2026.1.2 --revision 2 --output build/elastic-release --release-tar build/elastic-artifact/release.tar
python -m installer_prototype.dashboard_packages --publisher advantagescope --target linux-x64 --release v26.0.2 --revision 2 --output build/advantagescope-release --release-tar build/advantagescope-artifact/release.tar
```

Use native runners for the target platform. `--season` defaults to `2026` and
`--channel` to `real-packages`; set these to match the deployment registry. Set
`--revision` above that publisher repository's existing revision (the default is
the release's one-based position in the lock, for the disposable acceptance feed).

The output contains `spec.json` and a `payload` directory. Each optional
`release.tar` can be uploaded as the input artifact for
[`publisher-release.yml`](../.github/workflows/publisher-release.yml), with
`spec.json` as its specification. Configure that publisher's existing TUF trust,
archival signing key, and R2/Artifactory destination as described in
[CI signing](../deployment/ci-signing.md). These recipes do not publish externally.

Installed paths are `tools/elastic/` and `tools/advantagescope/`. On macOS, the
original named `.app` is preserved inside that directory. Project licenses and
asset provenance go in `licenses/<publisher>/`, outside the signed app bundle.
No source files inside the application are modified. Recipes check PE, ELF, or
Mach-O architecture; macOS additionally requires local `codesign --verify --deep
--strict` to succeed before and after packaging/installing. This checks signature
integrity without online revocation or Gatekeeper/notarization assessment.

AdvantageScope also carries signature data in macOS extended attributes. The
recipe retains the tarball’s PAX attributes in an owned JSON file, normalizes
redundant AppleDouble entries, and generates a fixed restore script. Qt IFW does
not restore these attributes itself, so a component `Execute` operation runs that
script with `/bin/sh` and `/usr/bin/xattr` after extraction. Attribute values are
restored unchanged, without signing keys or re-signing the app. They are attached
to Qt-owned files and disappear when those files are removed. The native gate
checks each declared attribute hash and size, then verifies the complete app
signature. JSON/script handoff keeps this metadata intact even when a publisher
repository is generated on Linux.

Archive extraction rejects traversal, duplicate names, special files, hardlinks,
escaping/dangling/cyclic links, and archive writes below a symlink. Contained
relative framework links are preserved. Qt payload archives use `tar.gz`; signed
publisher manifests record installed file hashes/sizes, executable bits, link
targets, and signature-attribute hashes/sizes. Release artifact handoff also preserves these links and modes.

## Native offline acceptance

```sh
python tools/bootstrap_ifw.py
python tools/check_dashboard_releases.py --ifw .tools/ifw
```

The checker downloads only the host platform's two release pairs, generates
throwaway independent publisher and authority TUF repositories, and assembles
signed baseline/current offline bundles. It then invokes the production native
acceptance checker on the exact candidate ZIP. No production signing/storage
credentials are used.

It installs both dashboards from empty caches, checks the `elastic`,
`advantagescope`, and combined `tools` profiles, installs the baseline, upgrades
both actual versions from the candidate bundle, compares every installed payload
against its signed manifest and a fresh install, checks obsolete-file removal,
and verifies that older media cannot downgrade either app. Hash checks are against
the signed source inventory, so a consistent extraction error in both fresh and
upgraded installations still fails. The report binds the candidate and baseline
ZIP hashes, native target, suite version, upstream versions, and file counts.

The entire install/maintenance worker has networking blocked by a Linux namespace,
macOS sandbox, or Windows program firewall rules. Downloads/package construction
happen before isolation. The worker runs `codesign` inside the macOS sandbox.
Windows rules cover Python and Qt; no dashboard process is launched by this gate.

[`dashboard-releases.yml`](../.github/workflows/dashboard-releases.yml) runs on
relevant pushes/PRs and manual dispatch on all four native runners. Logs,
provenance, and acceptance reports are retained as `real-dashboard-results-<runner>`
artifacts for 14 days. The large application payloads, test private keys, and
throwaway installation trees are not uploaded. The ordinary prototype regression
matrix still runs separately.

This validates real installation and maintenance payloads. GUI startup, renderer
behavior, robot connectivity, first-run asset downloads, shortcuts, OS integration,
quarantine/notarization policy, and applications running during an upgrade remain
separate acceptance work. It is not a claim that all application features function
offline. Production bundle publication continues to require its exact candidate
checks and configured storage/signing workflows.
