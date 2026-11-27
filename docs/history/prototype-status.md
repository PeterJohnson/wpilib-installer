> Historical evaluation and evidence. Commands describe that earlier revision. Use the [current development guide](../../CONTRIBUTING.md) and [production-readiness plan](../production-readiness.md) for active work.

# Prototype scope and findings

## Rust single-application work

The selected Rust/private-Qt direction now has an [online/offline application prototype](rust-manager-prototype.md), shared GUI/JSON CLI backend, pinned archival verification, embedded engine, persistent manager and cache-only maintenance. The native workflow tests fixtures and real dashboards on four OS/architecture runners. Native tough registry/publisher refresh, verified caches and offline-to-online updates are integrated. Release signing, self-update and OS integration remain unfinished; this does not supersede the Python reference behavior described below.

The [native online run](https://github.com/PeterJohnson/wpilib-installer/actions/runs/46051023760) passed 272 fixture/real-package checks plus 11 Rust unit tests on each of Windows x64, macOS ARM64, macOS x64 and Linux x64. [Saved evidence](../evidence/rust-online-2026-11-27.json) binds these results to commit `ef768bf`; [reproduction and scope](rust-manager-prototype.md#online-validation--november-27-2026) describe the HTTP/TUF tests.

The Python/Qt reference was implemented November 26, 2026, against Qt IFW 4.11.0 (Qt 6.9.3); the Rust application work followed November 27.

The Rust application passed **136 native lifecycle checks across Windows x64, Linux x64, macOS x64 and macOS ARM64**, plus seven Rust unit tests per runner. Checks include rendered GUI startup, real Elastic/AdvantageScope upgrades, cached maintenance with original media removed, and tamper rejection under native network isolation. [Results, artifacts and limits](rust-manager-prototype.md#validated-results--november-27-2026).

## Single-application distribution requirement

The November 27 [distribution evaluation](manager-distribution-evaluation.md) records the required one-application UX (at most one GUI plus one CLI). It compares Go and Rust with a private Qt engine, plus an integrated Qt/Rust application. Linux x64 native probes passed **69 checks (34 Go, 35 Rust)** using go-tuf 2.5.0 and tough 0.24.0: existing dashboard catalogs, updates/rollback, verified and tampered caches, interrupted 1,030-root histories, native archival authorization and old-offline-to-online trust continuity. Both require application adapters; tough needs transport-error normalization. See [results and scope](manager-distribution-evaluation.md#native-library-interoperability-results).

Those probes test trust interoperability only. The subsequent Rust application above implements the offline distribution/maintenance experiment; it is separate from both the probes and the Python reference. Signed-manager distribution, online integration and the other production gates remain open.

## Implemented

- Real Elastic and AdvantageScope WPILib release recipes with pinned upstream assets/licenses, portable payloads, architecture validation, macOS signature-integrity checks, and a separate four-runner offline installation/upgrade workflow. See [real dashboard packaging](../../packaging/README.md).
- Contained framework symlink preservation, executable-mode and per-file digest inventories, preservation/restoration of upstream macOS signature attributes, and native acceptance against signed source contents, including obsolete-file removal.

- Python TUF 7.0.1 online verification, independent publisher/authority roots and role keys, authenticated catalogs, consistent snapshots, current-clock expiry, persistent rollback state, verified cache fallback, and sequential root rotation. Threshold root setup/rotation tooling defaults to two of three root keys.
- Online authority trust is independent of the archival authority pin. Registry cache evidence is reverified through TUF, CI renewal supports a replacement archival key, and existing offline bundles retain their original key/policy.
- Root histories longer than 256 versions use bounded, resumable download batches with complete signature replay from the original bootstrap. The client reports retryable pending status after its per-operation budget; no catalog is accepted at a batch boundary. Publication-origin verification traverses the complete root chain.
- Timestamp-last TUF release publication and independent daily publisher/authority renewal workflows using Actions secrets. Existing R2 and Artifactory staging/promotion backends remain supported; no shared publisher metadata job is required.
- Schema-3 offline bundles with explicit `wpilib-archive-v2` indefinite authorization, TUF assembly provenance, no wall-clock or network requirement, and no changes to online trust caches during offline use. Earlier v1 media retains its bounded expiry and is never silently reinterpreted.

- Public offline ZIP and browser-page publication to R2/S3 or Artifactory. Immutable ZIPs, signed details, and permanent pages are verified before switching the single live HTML entry point. Previous downloads are retained; failed assembly/promotion/verification preserves the prior selection.
- Destination-serialized offline publication, authenticated revision floors, public URL/rendering checks, idempotent retries, lost-acknowledgement recovery, and conditional S3/R2 multipart uploads.
- Required Windows, macOS (Intel and ARM), and Linux offline lifecycle checks before any bundle publication. Separate native jobs install each exact candidate with empty caches, check every profile, upgrade the last successful bundle, compare inventory/payloads, and reject old-media downgrades. Signed candidate plans bind the ZIP and upgrade baseline; publication verifies the matching report and rejects a changed live baseline. Windows/Linux ARM64 publication fails closed until native tools/runners are supplied.

- Secret-backed reusable publisher release and scheduled registry renewal Actions
  workflows, with configurable S3/R2 publication or Artifactory staging/promotion,
  explicit first initialization, and immutable revisions. R2 retains conditional
  head updates; Artifactory uses a destination-based serialized Actions writer.
  Renewal preserves the current
  approved configuration and recovers from expiry without resetting history.
- Same-run tar artifact handoff preserving executable modes, separate signing
  roles, temporary PEM cleanup, and metadata-only reads of publisher history.
- Automatic authority-authenticated registry refresh, verified persistent cache,
  online expiry and revision checks, and explicit cached fallback on transport
  failure. New publishers/profiles require no executable or bootstrap change.
- Bundle-pinned registry identity/revision/hash and authority-signed archival
  authorization. Old media uses its original registry without network access or
  rollback of the online cache.

- Registry-driven publisher discovery, publication, precedence, and profile selection. The demo includes core, Elastic, AdvantageScope, and RobotPy. Independent repository generation and
  immutable publication directories. Atomic per-publisher index replacement,
  publisher-specific signing keys, component ownership checks, and local writer locks.
- Numeric component versions and cross-repository minimum-version dependencies.
  Deterministic compatible-set selection, including joint upgrades and holdbacks.
- Native online-only Qt installers and their persistent maintenance tools.
  Registry-defined C++, Java, Python, and tools profile defaults; Qt's individual component selection.
- Publisher-signature and SHA-256 verification before Qt consumes metadata or
  payloads, a lazy loopback gateway, and a content-addressed cache.
- Reproducible per-platform ZIP bundles containing the configured local repositories,
  signed manifests, exact source revisions, and a static download page.
  Failed assembly leaves the prior latest link intact.
- Verified local-only maintenance, no downgrade requests from older bundles,
  and a JSON update-discovery command backed by Qt's installed inventory.
- Pinned Qt downloads and native CI definitions for the three operating systems;
  assembly of fixture bundles for all six OS/architecture identities.

## Locally demonstrated

On Linux x64, the real Qt installer installed the tools profile over HTTP, fetched
its runtime dependency from the core repository, and did not fetch C++ or Java
payloads. Publishing Elastic revision 2 changed no other repository. The JSON
adapter detected the update, and the maintenance tool upgraded Elastic alone.

Offline tests stopped the source server and ran each installation/maintenance
command under `unshare -Urn`, with no network interfaces available. They exercised
fresh Java installation, upgrading Elastic from a newer bundle, preserving a newer
installation when given an older bundle, and removing/reinstalling a tool while
retaining its shared runtime. A separate C++ fresh installation was also exercised.

A native registry-update test installs from an authenticated three-publisher bundle, automatically discovers registry revision 2 adding RobotPy, adds the Python profile using the unchanged executable/bootstrap, and then installs from the old bundle with networking isolated while retaining revision 2 in the online cache.

Additional native tests install the fourth publisher’s Python profile from an offline bundle, then update its components through the online gateway. A single custom publisher with none of the original project names also publishes, selects, and bundles successfully. These are marker payloads, not a real Python/RobotPy installation.

The new publication acceptance runner also passed locally on Linux x64, with networking blocked for the complete worker. It installed all components and all four profiles from cold caches, upgraded Elastic 1.0.0 to 2.0.0 from exact signed ZIPs, compared installed file hashes with a fresh install, rejected old-media downgrades, and exercised first-publication reinstall behavior. The complete 103-test suite passed locally, including TUF-backed publication fixtures, indefinite archival authorization, archival-key rotation, and long root histories. Hosted-runner validation is recorded below.

The real `repogen --update` regression test used only old `Updates.xml`, its named
combined metadata archive, and one new component. The unchanged component remained
in the index without its old payload being present locally.

Unit/native tests additionally cover signature tampering, publisher isolation,
path traversal, file ownership collisions, corrupt cache/media, numeric version
ordering, compatibility holdbacks, and reproducible bundle output.

CI publication tests cover fresh-runner Qt generation without old payload reads,
interrupted uploads, competing writers, skipped abandoned registry revisions,
expired-head renewal, key cleanup, archive permissions, and the S3 SDK conditional
request contract. Artifactory REST tests cover staging verification, exact-path
promotion, partial/corrupt copies, immutable collisions, changed heads, configured
writer guards, and staged-registry recovery. Native Qt releases exercise both
storage contracts without old archive downloads. Live Artifactory/cloud publication
has not been run. Native GitHub Actions execution is recorded below.

TUF tests also demonstrate HTTP installs/updates, rejected expired online metadata, and native offline installation with archival verification evaluated in the year 2100, followed by an online upgrade after archival-authority rotation with the unchanged original bootstrap. Separate tests check cold/warm caches, no wall-clock dependency, signed policy enforcement, unchanged online history, authentic expired cache evidence, 520 root versions with expired intermediates and threshold rotation, and resumable 1,030-version catch-up.

Native macOS ARM64 is exercised on its own hosted runner. Generating
target-labelled data archives does not prove native Windows/Linux ARM64 support.
The GUI controller is included; visual/layout acceptance remains a manual gate.

## GitHub Actions validation

[Prototype CI run 45954840264](https://github.com/PeterJohnson/wpilib-installer/actions/runs/45954840264)
validated commit `7939d02` on November 27, 2026 (UTC):

| Runner | Native target | Result |
| --- | --- | --- |
| `windows-2022` | Windows x64 | 103 tests, offline publication fixture gate, and installer packaging passed |
| `macos-15` | macOS ARM64 | 103 tests, offline publication fixture gate, and installer packaging passed |
| `macos-15-intel` | macOS x64 | 103 tests, offline publication fixture gate, and installer packaging passed |
| `ubuntu-24.04` | Linux x64 | 103 tests, offline publication fixture gate, installer packaging, and six fixture bundles passed |

The native tests exercise online installation, offline installation and upgrades,
registry discovery, independent publisher updates, archival-key rotation, and TUF
root histories beyond 256 rotations. The publication fixture installs all profiles
with networking blocked by the host OS, upgrades Elastic from 1.0.0 to 2.0.0,
compares installed file hashes, and rejects old-media downgrades. It calls the same
checker used by the production offline workflow; no production release is
published during this test run.

The first runs identified missing Ubuntu XCB runtime libraries and Windows text
mode changing temporary signing-key line endings. The workflows now install the
required libraries, build the installer before the test suite to catch missing
runtime dependencies early, and preserve PEM bytes on Windows. The key cleanup
test covers both LF and CRLF input. The long root-history tests account for most
of the Windows test runtime; the complete suite takes roughly 11 minutes there.

Each runner uploads its native installer as `installer.tar.gz` in
`prototype-<runner>` and test logs/JSON acceptance reports in `test-results-<runner>`.
The macOS archive preserves the `.app` structure and executable modes. That run
used synthetic markers; real dashboard validation is recorded below. GUI
acceptance, manager code signing/notarization, full SDK payloads, and live
R2/Artifactory publication remain untested.

## Real dashboard validation

[Real dashboard releases run 45959389025](https://github.com/PeterJohnson/wpilib-installer/actions/runs/45959389025)
passed on commit `65530df`, November 27, 2026 (UTC). It installed and upgraded the
official WPILib distributions of Elastic **2026.1.1 → 2026.1.2** and AdvantageScope
**26.0.1 → 26.0.2** from signed offline bundles.

| Native target | Result | Elastic owned entries checked | AdvantageScope owned entries checked |
| --- | --- | --- | --- |
| Windows x64 | Passed with program firewall isolation | 192 | 139 |
| macOS x64 | Passed with network sandbox isolation | 247 | 355 |
| macOS ARM64 | Passed with network sandbox isolation | 247 | 355 |
| Linux x64 | Passed with network namespace isolation | 183 | 137 |

Every target exercised fresh installation, both individual dashboard profiles and
the combined tools profile, baseline installation, real version upgrades, signed
source-content verification, comparison with a fresh install, obsolete-file
removal, and rejection of downgrades from old media. Every report binds the exact
candidate and baseline ZIP hashes. Both Mac targets additionally verified the
original app signatures before packaging and after offline installation/upgrades.

The Mac tests found that Electron resources carry signature data in extended
attributes. Preserving ordinary file bytes and framework symlinks was insufficient:
Qt's extraction did not restore these attributes. The recipes now preserve the
upstream values in package-owned metadata and restore them with native `xattr`
commands after Qt extraction, without re-signing. The gate checks the declared
attribute hashes/sizes and requires `codesign --verify --deep --strict` to pass.

The [110-test regression matrix](https://github.com/PeterJohnson/wpilib-installer/actions/runs/45959389049)
also passed on the same commit and four runners; native attribute tests run on
macOS, with platform-specific tests skipped where inapplicable.

Logs, provenance, and reports are available as `real-dashboard-results-<runner>`
Actions artifacts. See [packaging commands and recipe contracts](../../packaging/README.md).
Application GUI startup/functionality, robot connectivity, first-run downloads,
Gatekeeper/quarantine and notarization assessment, and production publication
remain outside these installation/upgrade checks.

## Deliberate prototype limits

| Area | Remaining work before production |
| --- | --- |
| Real payloads | Elastic/AdvantageScope release recipes are implemented; validate deployment policy and application behavior. Import allwpilib, JDK/toolchains, standalone Python, pinned RobotPy host/robot wheels, licenses, and full offline build dependencies. |
| Setup operations | Existing paths, launcher/shortcut integration, Gradle caches, VSIX installation, running-process checks, and legacy migration. |
| Manager distribution | Meet the [single-application UX and gates](manager-distribution-evaluation.md): responsive GUI with no separate runtime/engine acquisition, persistent verified maintenance, shared CLI policy, clean-machine dependencies, signing and recoverable self-update. Select a native client only after interoperability and full UX evidence. Direct manual invocation of Qt currently bypasses adapter checks. |
| Trust | TUF client verification, online role separation, long root-history catch-up, archival-key-independent online updates, and explicit indefinite archival authorization are implemented. Remaining work: emergency threshold-compromise recovery, distribution of replacement offline trust pins, release-withdrawal administration, hardened signing services, and deployment monitoring. The client assumes its local trust cache and the publishing workflow's authoritative origin are protected from unauthorized replacement. |
| Compatibility | Season policy, upper bounds/conflicts, SDK release groups, historical retention, installed-version-aware planning, richer diagnostics, and explicit unsupported package/platform availability. |
| GUI | Dedicated profile choice, source/bundle picker, user-facing update summaries, visual/accessibility acceptance. Current launch flags select profile and source. |
| Transactions | Crash/power-loss recovery, repair, file replacement on Windows, custom operation rollback, and disk-space tests. Qt's built-in file operations alone do not prove all of these. |
| Deployment | Configure and run the implemented secret-backed publisher/registry workflows against actual upstream repositories and HTTPS storage. Smoke-test R2 conditional writes or Artifactory promotion and enforce its designated writer/token isolation. Native publication gates pass on all four supported hosted runners with fixtures; run them with real payloads, and provision enough runner disk/artifact quota for candidate and baseline archives. S3/R2 supports conditional multipart uploads; Artifactory uses server/proxy limits and a 120-second HTTP timeout. |
| VS Code | Consume the JSON protocol and launch the manager from an extension. Editor acquisition/redistribution remains as described in the proposal. |
| Platforms | Run GUI tests on each platform; supply and test native Windows/Linux ARM64 IFW builds; signing/notarization and minimum OS baselines. |

## Source references

- [Qt repository generation and installer tools](https://doc.qt.io/qtinstallerframework/ifw-tools.html)
- [Qt CLI and temporary repository list](https://doc.qt.io/qtinstallerframework/ifw-cli.html)
- [Qt configuration and component aliases](https://doc.qt.io/qtinstallerframework/ifw-globalconfig.html)
- [Qt IFW 4.11 release and distribution](https://www.qt.io/blog/qt-online-installer-and-qt-installer-framework-4.11.0-released)

The tools lock uses Qt's public Linux x64, Windows x64, and universal macOS package
archives with independently calculated SHA-256 values. No Qt sources are modified.
