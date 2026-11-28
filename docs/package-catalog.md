# Package catalog development and producer CI

Build the catalog in parallel with the eventual producer integrations. During development, create real packages from the last released legacy installer and/or its published Maven and upstream artifacts. Do not require every producer to change its CI, or rebuild every project from source, before testing the new installer. In parallel, develop the shared packaging scripts and GitHub Actions interfaces that the respective repositories will invoke after their normal release builds.

This is the catalog implementation plan. The dashboard importers and reusable publisher workflow already exist; general legacy-installer/Maven importers and producer-facing packaging actions still need implementation. Current core SDK and RobotPy fixtures are not real catalog packages.

## One package definition, multiple input sources

Each component should have one reviewed definition for its stable ID, publisher, target, installed paths, dependencies, version mapping, licenses, and installation/removal operations. Source adapters obtain and normalize the inputs; the common recipe produces the package. Switching from imported release artifacts to producer build outputs must not change the component identity or installation contract.

```text
Pinned legacy release contents ─┐
Pinned Maven/upstream artifacts ├─> common component recipe ─> spec.json + payloads
Producer CI build outputs ──────┘                            └─> release.tar
                                                                  │
                                                   independent publisher workflow
                                                                  │
                                                    TUF repository / offline bundle
```

The common output is the existing publisher input contract: `spec.json`, relative payload directories, and a tar handoff preserving executable modes and contained links. Recipes also record source provenance and any transformations. The same package validation, publication tooling, client, and offline assembler consume either source. Maven remains a packaging input; users download larger combined installer packages, not individual Maven artifacts.

Do not create temporary public component IDs for imported packages. Development repositories use separate trust and publication destinations; the component IDs are the intended final ones. Existing synthetic fixture IDs do not, by themselves, settle the production package boundaries. Decide the real catalog boundaries before making a migration promise for installed development builds.

## Development inputs

Select the last released legacy installer when implementing the first import and commit the exact release tag, source revision, platform asset URLs, lengths, and SHA-256 digests to a reviewed input lock. “Last release” describes how that baseline is selected, not a floating URL resolved every build. Refresh it through explicit lock updates. Where possible, also pin a preceding compatible release for actual upgrade coverage.

Extract the released resource and artifact archives, without running the old installer to populate the packaging host. Audit the configuration and setup code at that exact legacy release tag; the current legacy checkout is useful for discovery but is not proof of a release's contents. Preserve upstream executable bytes, modes, contained symlinks, app signature metadata, licenses, and notices. Keep old installer executables and maintenance state out of the new payloads.

Where the release does not contain a required artifact, or a direct artifact is a better input, use pinned Maven/upstream downloads. The lock must include Maven repository URL, group/artifact/version, classifier, extension, and digest/length for every resolved artifact, including required POMs and parent/BOM metadata. Resolve the dependency closure from the chosen release's GradleRIO/build metadata and validate it offline; copying only top-level JARs is insufficient. Freeze snapshot content if unavoidable rather than resolving mutable snapshots during repeat builds. Record the precise dependency-resolution inputs and tool versions.

Download only the selected host platform's package set, plus platform-independent material and cross-compile target artifacts that those packages require. A Linux development installation can still need roboRIO-target libraries; filtering to the host classifier alone is incorrect. Use a verified download cache so repeat packaging can run without network access once inputs are present. Missing inputs must fail with their identities, not silently produce an incomplete package.

Do not blindly re-run the legacy installer's setup steps. Inventory them and assign each to a recipe transformation, an explicit reversible component operation, or future OS integration. Maven metadata generation, Gradle setup, VS Code settings/extensions, tool launchers, and shortcuts need this review. Generated metadata must have clear ownership, especially when multiple components share a Maven directory. User-edited configuration and project files are not disposable package payloads.

## Initial catalog work

The following are recipe families to implement, not a final list of component IDs. Keep independently updated components separate while making tightly coupled SDK closures explicit.

| Family | Development source | Eventual producer integration |
| --- | --- | --- |
| WPILib Java/C++ libraries, headers, native libraries, examples and documentation | Legacy release payloads and locked Maven closure matching its SDK/GradleRIO version | Release outputs from allwpilib and the relevant SDK producers, normalized by the same recipes |
| Gradle/GradleRIO offline support and vendor metadata | Legacy archives plus exact plugin, wrapper/distribution, dependency and vendor inputs | Producer build outputs or locked dependencies assembled by the owning release workflow |
| JDK and native toolchains | Legacy payloads or the exact upstream distributions named by its release configuration | A distribution-maintenance workflow using the same pinned upstream recipes; not every third-party dependency has a WPILib-controlled CI |
| VS Code and extensions | Legacy payloads where present, supplemented by pinned editor/VSIX assets | Extension producer CI plus a pinned editor-distribution recipe |
| WPILib desktop tools | Legacy or Maven release artifacts, including runtime dependencies | Respective tool/allwpilib release outputs |
| Elastic and AdvantageScope | Existing pinned official WPILib release adapters | Each project's release CI, preserving its independent repository and schedule |
| RobotPy | Pinned standalone Python and compatible wheel/runtime closure; do not assume it exists in the legacy installer | RobotPy's release workflows using the same Python/runtime recipes |

For shared files, choose one owner and declare dependencies instead of duplicating ownership across packages. Define version ordering and a packaging revision scheme before publishing: corrected or differently assembled content needs a new component version, even when the upstream version is unchanged. A higher repository revision alone does not make same-version replacement safe. Retain the original upstream version separately in provenance.

## Producer workflow interfaces

Develop small, versioned packaging actions/scripts alongside each importer. A producer should supply its already built, tested, and platform-signed outputs plus the recipe, target, version, and provenance. Packaging should not repeat the producer's build or require downloading the old monolithic installer. For dependencies not built in that repository, the recipe can continue consuming reviewed locked upstream inputs.

The packaging interface should:

1. Validate native architecture, required files, compatibility, licenses, signature metadata, and ownership using the common recipe.
2. Emit the existing `spec.json`/payload tree and `release.tar`, together with an inventory/provenance report; use native runners where signature verification requires them.
3. Run on PRs with disposable trust and no publication credentials. Package creation must be independently usable as a local script, so it can also drive import tests.
4. Hand the same-run artifact to the existing SHA-pinned [`publisher-release.yml`](../.github/workflows/publisher-release.yml) on authorized releases. Publication and renewal retain publisher-specific secrets, TUF roles, Artifactory/R2 behavior, and serialized index updates.

A publisher release currently supplies its complete current component set. A repository producing only part of a publisher's catalog must not replace the catalog with that subset. During integration, either compose that publisher's complete input in its owning workflow or give independently released components their own authorized publisher repository. Do not introduce multiple uncoordinated writers to one feed. Independent Elastic, AdvantageScope, and RobotPy publication remains the intended arrangement.

The producer-facing packaging action is new work; the reusable publication workflow is implemented today. See [CI signing and handoff](../deployment/ci-signing.md). Pin both the action/tooling and reusable workflow to reviewed immutable revisions when integrating them into producer repositories.

## Equivalence and acceptance

Equivalence means the same supported installed functionality and component contract, not necessarily byte-identical compressed archives. For inputs from the same upstream release, compare installed file inventories, hashes, modes, links, signature metadata, dependencies, and licenses between import and producer-output paths. Explicitly record allowed differences such as source provenance and regenerated metadata. Do not claim equivalence to a release by comparing only two newly generated inventories; use the locked upstream contents and expected layout as the reference.

For each recipe, add its import/build-output parity checks and native installation tests together. Test fresh installation, a real older-to-newer upgrade, removal and reinstallation from retained caches after deleting original media, and dependency-removal protection. Include an upgrade pair with removed/renamed files, or a separate targeted case if the real pair does not have them. Preserve package-owned/user-owned file boundaries.

SDK acceptance must also compile representative Java and C++ projects with networking disabled and no pre-existing user Gradle/Maven caches, using the installed toolchain and package closure. RobotPy needs equivalent offline Python import/build or tooling checks against its bundled runtime and wheels. Installation hashes alone do not establish a usable development environment. Physical-device deployment and application-specific integration remain separately identified coverage.

## Implementation order

1. Lock the chosen legacy release and inventory its package contents and setup behavior per supported platform. Record missing externally downloaded inputs explicitly.
2. Implement one real SDK recipe end to end with both a release-import adapter and a local producer-output adapter. Emit the current publisher handoff and establish offline functional acceptance.
3. Wrap that recipe in a producer-facing action/example workflow while expanding the rest of the catalog through the same pattern. Keep existing dashboards on their working real-package path.
4. Validate upgrades from imported packages to packages built from producer outputs in development feeds. Coordinate version allocation and activate each producer's publication independently when ready.
5. Retain the pinned import path for reproducible historical baselines and regression tests after routine publication moves into producer CI.

Importing payloads from an old release does not migrate an existing legacy installation in place. That installation migration, final seasonal paths, OS integration, and full uninstall remain separate lifecycle work in the [production-readiness plan](production-readiness.md).
