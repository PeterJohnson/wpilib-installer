# WPILib installer prototype

A runnable Qt Installer Framework 4.11 prototype for independently published
WPILib core, Elastic, AdvantageScope, and RobotPy repositories, with additional publishers configured in the trusted registry. It installs and updates
components through Qt's native engine, verifies publisher manifests before Qt
reads them, and assembles complete offline bundles for one platform at a time.

**The demo installs small synthetic text payloads, not WPILib or either dashboard.**
This repository proves the distribution and maintenance paths. Production SDK
packaging, platform integration, and the remaining feasibility gates are tracked
in [implementation status](docs/prototype-status.md). The full
[architecture proposal](docs/installer-architecture.md) is included.

## Prerequisites

- Python 3.11 or newer, with `cryptography` installed through this project.
- Qt Installer Framework 4.11.0. Use an existing installation or the pinned
  download helper below, which requires `7z` or `7zz`.
- On Ubuntu 24.04, install Qt runtime libraries and the archive extractor:
  `sudo apt-get install 7zip libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-cursor0 libxcb-shape0 libxcb-xfixes0 libxcb-sync1 libx11-xcb1`.
- Native Qt tools for the host on which you build an installer. The pinned downloads
  cover Linux x64, Windows x64, and universal macOS. Windows/Linux ARM64 native
  installer builds remain a feasibility gate. Package/bundle target identities
  cover all six combinations.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python tools/bootstrap_ifw.py
```

On Windows activate `.venv\Scripts\Activate.ps1` and use `python` for the commands.
The download helper verifies the checked-in SHA-256 and extracts into `.tools/ifw`.
Qt binaries and generated private keys are ignored by Git.

## Run a local demo

The commands below use Linux x64. Substitute `windows-x64` and an `.exe` installer
name on Windows; on macOS use `macos-arm64` or `macos-x64` and an `.app` name.

```sh
python -m installer_prototype demo --output build/demo --target linux-x64 --ifw .tools/ifw --base-url http://127.0.0.1:8765
python -m installer_prototype build-installer --ifw .tools/ifw --output build/wpilib-installer
python -m http.server 8765 --bind 127.0.0.1 --directory build/demo/store
```

Leave the static server running. In another terminal, activate the environment and
open Qt's component-selection GUI with C++ defaults:

```sh
python -m installer_prototype run --registry build/demo/registry-source.json --engine build/wpilib-installer --profile cpp
```

The demo registry defines `cpp`, `java`, `python`, and `tools` profiles. Profile names and component lists come from the registry, including GUI defaults passed at launch; they are not compiled into the installer. Qt's component page allows
individual changes. The prototype chooses the profile at launch; a dedicated
profile-selection page is still to be implemented. The C++ and Java profiles depend on the shared runtime fixture; tools acquire only their declared runtime. The Python fixture selects a standalone-runtime marker, RobotPy SDK marker, and deployment marker. It does not install actual Python or wheels.

For an unattended test installation into a disposable directory:

```sh
python -m installer_prototype run --registry build/demo/registry-source.json --engine build/wpilib-installer --root build/installed --command install --profile cpp --headless
```

`--headless` accepts the demo licenses/messages and confirms the command. Without
it, CLI commands can prompt. Launch through the Python adapter; the generated Qt
binary deliberately has no default remote repositories. Qt owns `components.xml`
and the maintenance history; there is no second mutable component database.

## Publish an independent tool update

The demo creates producer specifications under `build/demo/specs/`. A specification
contains publisher, season/channel/target, increasing repository revision, and
packages with IDs, numeric versions, dependencies, and local payload directories.
`payload` is relative to the specification. The fixture generator can make a second
Elastic release:

```sh
python -c "from installer_prototype.demo import write_spec; write_spec('build/demo/specs', 'elastic', 'linux-x64', 2)"
python -m installer_prototype publish --spec build/demo/specs/elastic-2.json --store build/demo/store --registry build/demo/registry-source.json --key build/demo/keys/elastic.pem --ifw .tools/ifw
python -m installer_prototype check-updates --registry build/demo/registry-source.json --root build/installed
python -m installer_prototype run --registry build/demo/registry-source.json --engine build/installed/maintenancetool --command update --headless org.wpilib.elastic
```

Only Elastic's repository changes. The JSON command provides a versioned update
discovery interface for a future VS Code extension; the extension itself is not
implemented. Use `--engine build/installed/maintenancetool` without `--command` to
open maintenance. On Windows/macOS append `.exe`/`.app`.

`publish` creates a complete immutable revision of **one publisher's** repository.
Multi-component publishers supply their complete current component set. The
native regression test separately verifies the sparse `repogen --update` workflow
from the proposal; the publication command does not yet optimize multi-component
releases that way. This command uses local storage. The reusable
[publisher release workflow](.github/workflows/publisher-release.yml) uploads to
Cloudflare R2/S3 or Artifactory, using a publisher-specific Actions signing secret.
R2 retains conditional index replacement; Artifactory uses verified staging and
server-side promotion with a serialized writer per destination. See
[CI deployment](deployment/ci-signing.md) and [Artifactory setup](deployment/artifactory.md).

## Assemble and use an offline bundle

```sh
python -m installer_prototype bundle --registry build/demo/registry-source.json --output build/downloads --key build/demo/keys/bundle.pem
python -m http.server 8766 --bind 127.0.0.1 --directory build/downloads
```

Open `http://127.0.0.1:8766` in a browser to download the latest compatible
single-platform ZIP. Copy it, the matching installer, and the trusted `registry-source.json` bootstrap to
the disconnected machine; extract the ZIP. The prototype adapter also needs its
Python environment there. A self-contained manager distribution is future work.

```sh
python -m installer_prototype run --registry build/demo/registry-source.json --bundle /path/to/extracted-bundle --engine build/wpilib-installer --root build/offline --command install --profile java --headless
python -m installer_prototype run --registry build/demo/registry-source.json --bundle /path/to/newer-extracted-bundle --engine build/offline/maintenancetool --command update --headless
```

Offline mode uses the authority-signed registry embedded in the bundle, verifies its archival authorization and the signed bundle binding, and verifies all pinned publisher manifests/payloads, and passes only verified `file:///` repositories to
Qt. An older bundle never requests a downgrade. Cache contents cannot conceal
missing or corrupt USB media. The fixture packages have no scripts that access
the network.

The assembler considers up to eight revisions per repository and chooses the
first compatible combination using `repository_order` from the registry (alphabetical publisher IDs if omitted), with descending revisions within each publisher. It stops with an explicit error after 4,096 combinations rather than searching an unbounded product.
This is an explicit prototype policy, not a general dependency solver. Newer
incompatible tool releases are recorded as held back. Payloads are cached by hash;
historical payloads and other platforms are excluded from the ZIP.

## Validation and CI

```sh
python -m unittest discover -v
IFW_PATH=.tools/ifw python -m unittest discover -v
IFW_PATH=.tools/ifw IFW_TEST_NETWORK_NAMESPACE=1 python -m unittest tests.test_ifw -v
python tools/build_demo_bundles.py --ifw .tools/ifw
python tools/check_offline_fixture.py --ifw .tools/ifw --work build/offline-check --report build/offline-report.json
```

The third command is Linux-only and uses `unshare -Urn` for each offline native
operation. Run it on a host allowing unprivileged user/network namespaces. On
other platforms the tests stop the source HTTP server before offline operations.
In PowerShell set `$env:IFW_PATH='.tools/ifw'` before invoking unittest.

[Prototype CI](.github/workflows/ci.yml) runs on each push, pull request, or manual
dispatch with Windows x64 (`windows-2022`), macOS ARM64 (`macos-15`), macOS Intel
(`macos-15-intel`), and Linux x64 (`ubuntu-24.04`). Every job builds a native
installer, runs the complete test suite, and exercises the same network-blocked
offline fixture gate required by publication. No release secrets or storage
configuration are needed.

Each successful job uploads `prototype-<runner>` with `installer.tar.gz`; the tar
preserves executable modes and the macOS `.app` directory structure. Linux also
uploads six separate target-labelled fixture bundles. `test-results-<runner>`
contains the test log, offline gate report, and Qt operation logs, including on
failure when those files exist. These artifacts use synthetic payloads and are
not signed production distributions. See [recorded results](docs/prototype-status.md#github-actions-validation).

The [offline assembly workflow](.github/workflows/offline-bundles.yml)
is triggered by producer notifications or a reconciliation schedule once
[deployment registries](deployment/registries/README.md) are configured. It emits
verified ZIPs and browser download pages to R2/S3 or Artifactory only after required
Windows, macOS, and Linux offline lifecycle tests and native validation of each
exact candidate ZIP. Validation covers all components/profiles, upgrading the
last published bundle, installed payloads, and protection against old-media
downgrades. Native checks use Linux network namespaces, a macOS network sandbox,
or Windows firewall rules for Python and Qt. Candidate/baseline ZIPs are retained
briefly as Actions artifacts for the job handoff; reports and publication audit
metadata are also retained. Configure [offline download hosting](deployment/offline-bundles.md)
and its storage secrets to enable it. Windows/Linux ARM64 publication is blocked
until native Qt tools and acceptance runners are added.

The [registry renewal workflow](.github/workflows/registry-renewal.yml) renews the
currently approved signed registry before expiry and supports explicit publication
of reviewed configuration changes. The publication workflows use separate
Actions signing/storage secrets, preserve immutable history, and publish mutable
heads last. Configure them using the [signing guide](deployment/ci-signing.md).

## Add a publisher

Add a repository entry to the trusted registry with a safe lowercase publisher ID,
HTTPS URL, pinned Ed25519 public key, exact component allowlist, and optional
`minimum_revision`. Add its name to `repository_order` if that field is present.
Profiles may then refer to its component IDs. Publish with the existing `publish`
command; online selection and offline assembly discover it from configuration.
No publisher names are built into the engine, adapter, or bundle verifier.

See [the registry contract](deployment/registries/README.md) for the schema and
ordering/completeness rules. The demo's four publishers and profile recipes are
example data in `installer_prototype/assets/demo.json`.

Publish an authority-signed registry revision after approving configuration changes:

```sh
python -m installer_prototype publish-registry --registry build/demo/registry.json --source build/demo/registry-source.json --key build/demo/keys/registry.pem --revision 2 --output build/demo/store/linux-x64/registry
python -m installer_prototype refresh-registry --registry build/demo/registry-source.json --cache build/cache
```

`registry.json` is the editable authority input. `registry-source.json` is the
stable client bootstrap; keep it unchanged when adding publishers. The demo creates
both files, separate archival keys, independent TUF keys/roots for every repository, and initial TUF catalogs. If a
demo predates this feature, generate a new demo directory to obtain these files.

`run`, `check-updates`, and `bundle` automatically refresh when `--registry` points
to a source bootstrap. Use the same persistent `--cache` directory across runs
(default `build/cache` in this prototype; use an absolute path for integrations).
Every cache read rechecks its TUF chain and catalog evidence. Network failures can use an unexpired
verified cache with a warning; invalid metadata cannot. Lower revisions and changed
content at the same revision are rejected. `refresh-registry` and `check-updates`
return registry status/revision in JSON.

Registry publication defaults to 7 days of online authorization (`--valid-days`)
and explicit indefinite archival authorization. Omit `--offline-days` for the v2
policy; it is only for bounded legacy media. Renew online authorization by publishing a
higher revision, even when the approved configuration is unchanged. This requires
the included daily authority renewal workflow. Separate daily publisher workflows renew TUF metadata.

New schema-3 TUF bundles contain `registry.json` and bind its revision/hash in their signed
manifest. `--bundle` uses that snapshot without any registry fetch. A revision-1
bundle remains usable after the same client has cached revision 2 with additional
publishers, with no offline expiration date under v2. Offline use never overwrites
the newer online cache. Missing/extra publishers are checked against the pinned set.

Manually trusted local registry files remain supported. Bundles built from them pin
that local configuration by hash without claiming an authority-issued revision.
Legacy bundles require their original matching local registry. Merely finding a
public key on USB never makes it trusted.

## Trust boundary

The online adapter uses **python-tuf**, with an independent root, targets,
snapshot, and timestamp for the authority and each publisher. TUF authenticates
release catalogs; their manifests bind every Qt metadata file and payload hash.
The client retains verified metadata history and checks current expiry before
exposing repositories to Qt. Publisher component allowlists remain enforced.

Online registry verification is independent of the archival authority key pinned
in an old installer. CI can rotate that signing key while existing clients keep
updating through TUF. Offline bundles still require their matching archival pin;
online trust does not silently replace it.

Root histories longer than 256 versions are processed in bounded batches, always
reverified from the original bootstrap. Up to 1,024 remote root requests are made
per operation. A longer catch-up saves progress and reports `root-update-pending`
with `retryable: true`; retry with the same cache. No catalog is authorized until
catch-up finishes and current TUF expiry checks pass.

Schema-3 offline bundles use the explicit `wpilib-archive-v2` policy with
`lifetime: indefinite`: a separate authority signature authorizes the pinned
publisher and bundle keys without an expiry date. The bundle binds that record
and explicit null expiry; offline verification needs no accurate clock or network.
Keep the matching trusted installer/bootstrap with the ZIP. Earlier v1 bundles
retain their signed expiry and need a one-time rebuild to adopt v2.
Disconnected clients cannot learn later revocations. Offline verification never alters
TUF cache history or skips online freshness checks. See [TUF setup, renewal, root
rotation, and archival policy](deployment/tuf.md).

The demo and native publication gates use TUF. HTTPS refresh and CI publication
require TUF configuration; legacy local fixtures remain for regression coverage.
Root keys stay outside routine CI. Archival authority-key migration and emergency
recovery remain administrative work. Local cache deletion/replacement is outside
the trusted-filesystem threat model.
Use generated demo keys only for disposable test repositories. Publisher scripts
would still have installer privileges; this is not a sandbox for untrusted code.

Project code uses the [BSD-3-Clause license](LICENSE.md). Qt IFW and its bundled
dependencies retain their own licenses; read the proposal before distributing a
signed production installer.
