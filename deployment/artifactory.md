# Artifactory staging and promotion

The publisher and registry-renewal workflows support Artifactory alongside Cloudflare R2. Select `storage.type: artifactory` in a publication configuration. Existing S3/R2 configurations keep working; omitting `type` still selects S3. Each publication selects one destination backend; automatically mirroring the same release to both services is not implemented.

## Repositories and credentials

Create two **Generic Local** repositories per publisher, for example:

| Purpose | Elastic example | Access |
| --- | --- | --- |
| Staging | `elastic-installer-staging-local` | Private; accessible by the publisher's release workflow |
| Release | `elastic-installer-release-local` | Public read for installers; publication credentials limited to the designated writer |

Use analogous pairs for core, AdvantageScope, RobotPy, and other publishers. Platforms/seasons/channels use prefixes within the pair. A registry authority may have its own pair or use R2 independently. The installer registry contains only release URLs. No virtual repository or Maven/PyPI layout is required. [JFrog repository types](https://docs.jfrog.com/artifactory/docs/repository-management-overview).

The workflow token needs read access to both repository roots and their relevant paths, deployment access to staging, and permission to copy into the release repository and replace its mutable head. Where permission patterns allow, reserve overwrite rights for `tuf/metadata/timestamp.json` (publisher or authority), keeping immutable revision paths create-only. The helper performs no delete/move operation. Scope the token to the relevant repository pair, and retain that token only in its writer repository's protected environment. Public readers need no token.

Promotion uses the **Copy Item REST API** on the exact files in a revision. It does not create JFrog build-info records or call build-status promotion, and it does not require Release Lifecycle Management. Copy Item requires an Artifactory subscription that includes that API (the documentation lists Pro). This narrow path-based operation avoids resolving a wider artifact set by build name or checksum. [Copy Item API](https://docs.jfrog.com/artifactory/reference/copyitem).

Store `ARTIFACTORY_ACCESS_TOKEN` in the producer's `publisher-release` environment, together with its archival `PUBLISHER_SIGNING_KEY` and three `TUF_*_KEY` secrets. For authority renewal, use `REGISTRY_ARTIFACTORY_ACCESS_TOKEN` and `REGISTRY_SIGNING_KEY` in `registry-authority`. The workflow maps the authority token to the helper's `ARTIFACTORY_ACCESS_TOKEN` environment variable. The helper removes the token from the child-process environment and uses it only in HTTPS Authorization headers. It refuses redirects so a login UI or CDN cannot receive the token.

## Configuration

Start with the [publisher example](publisher-publications/elastic-artifactory-linux-x64.json.example) or [registry example](registry-publications/artifactory-linux-x64.json.example):

```json
{
  "type": "artifactory",
  "api_url": "https://packages.example.org/artifactory",
  "staging_repository": "elastic-installer-staging-local",
  "release_repository": "elastic-installer-release-local",
  "prefix": "2027/stable/linux-x64",
  "public_url": "https://packages.example.org/artifactory/elastic-installer-release-local/2027/stable/linux-x64",
  "writer_repository": "OWNER/ELASTIC_REPOSITORY"
}
```

This object is the configuration's `storage` field. `api_url` must be the canonical, direct HTTPS Artifactory service URL, not its UI or a caching/CDN endpoint. `public_url` is the download root advertised in the signed registry; it can use a separate public hostname if that mapping is configured. The staging and release repository names must differ. The prefix is preserved exactly during promotion, with no repository-layout translation.

In the caller's reusable-workflow job, keep the existing input artifact, pinned tooling commit, and signing-key mapping. Change `publication_config` to the Artifactory file and supply:

```yaml
secrets:
  PUBLISHER_SIGNING_KEY: ${{ secrets.PUBLISHER_SIGNING_KEY }}
  TUF_TARGETS_KEY: ${{ secrets.TUF_TARGETS_KEY }}
  TUF_SNAPSHOT_KEY: ${{ secrets.TUF_SNAPSHOT_KEY }}
  TUF_TIMESTAMP_KEY: ${{ secrets.TUF_TIMESTAMP_KEY }}
  ARTIFACTORY_ACCESS_TOKEN: ${{ secrets.ARTIFACTORY_ACCESS_TOKEN }}
```

No S3 credentials are needed for that configuration. The [common signing guide](ci-signing.md) documents the complete caller job and first-publication procedure. Registry renewal automatically selects the backend from each discovered configuration and supports both scheduled renewal and explicit `apply` runs.

## Publication and serialization

The workflow calculates a destination key from the normalized API URL, release repository, and prefix. It uses `installer-publication-<key>` as the Actions concurrency group and passes the same key to the helper. The key excludes config filename, branch, workflow name, staging repository, and public download URL. Renaming or duplicating a configuration for the same destination therefore does not create a second writer group. Different publishers/platforms have different destinations and run independently.

**Each Artifactory destination must have one authoritative GitHub writer repository.** GitHub concurrency groups are repository-scoped. `writer_repository` records that owner, and the helper refuses publication unless it runs in that Actions repository with the matching serialization key. These environment checks are guardrails for the supported workflow, not proof of an externally held distributed lock. Protect the workflow/configuration and keep its token out of other repositories and manual writers. Other workflows in the same repository must use the exact same group. Do not configure alternate API hostnames for the same destination; arbitrary DNS aliases cannot be recognized as the same service automatically.

The job holds the group before reading the current index and throughout generation, staging, promotion, and final publication. It uses `queue: max`, `cancel-in-progress: false`; current GitHub Actions allows up to 100 waiting runs. Queue arrival order does not establish release revision order, so the helper still rejects a revision older than or equal to the live head. A queued old release cannot roll back a newer one. [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

For a publisher revision `42`, the job:

1. Verifies the origin TUF chain and catalog of signed release manifests. No old payloads are downloaded. Explicit initialization is required if no timestamp exists.
2. Generates and signs the complete new publisher snapshot as before.
3. Uploads its files under `<prefix>/releases/42/` in staging, using `X-Checksum-Sha256`. It compares stored SHA-256 and size for every staged file before promoting anything. Identical existing files can be reused; conflicting immutable bytes fail.
4. Checks existing release files for conflicts. Copies the exact signed file set using `api/copy`, preserving paths with `suppressLayouts=1` and `failFast=1`. It copies the signed manifest after the payloads. It never promotes a whole repository or includes the mutable timestamp in that set.
5. Checks every released file's stored SHA-256 and size. Even an HTTP-success response with missing/corrupt copies fails verification.
6. Stages, promotes, and verifies the new hash-prefixed catalog and versioned TUF metadata. Re-reads the live timestamp and compares its fingerprint with the starting head. An unexpected change fails; this is not atomic compare-and-swap. If unchanged, it replaces only `tuf/metadata/timestamp.json` last and verifies it.

The storage API's checksums are used to verify origin upload/copy results. Clients independently verify the publisher signature and hash the downloaded bytes. Promotion does not change signed content or re-sign it. [Deploy Artifact checksum validation](https://docs.jfrog.com/artifactory/reference/deployArtifact).

Registry publication uses the same sequence for `revisions/N.json` and its TUF catalog/metadata, followed by the live TUF timestamp. It skips authentic abandoned revisions found in staging as well as release, so a failed promotion cannot indefinitely block the next renewal. Old offline bundles keep their original registry revision and archival policy; v2 authorization has no expiry.

## Failure and deployment checks

An upload or promotion failure leaves the old TUF timestamp discoverable. Some copied files may remain public at unreferenced immutable paths; do not infer promotion is atomic across files. The next run never overwrites different bytes at those paths. A publisher run with a reserved signed revision must allocate a higher revision; registry renewal handles skipped revisions automatically. A final head-upload timeout can be ambiguous if the server committed before the connection failed: inspect the head before retrying. Any new head written by this workflow references an already verified complete revision.

The helper checks both repositories exist and are readable before accepting a missing artifact as an empty feed. Authentication/authorization errors, redirects, checksum failures, malformed responses, and promotion errors fail closed. Read access to repository-root metadata is required by that initial check.

Configure Artifactory/proxy/CDN rules to revalidate `tuf/metadata/timestamp.json` and the live download `index.html`, avoid long negative caching of new paths, and retain immutable revisions. The helper sends no-cache requests to the direct API, but does not configure public response headers or server caching policy. R2/S3 supports multipart uploads; Artifactory upload limits depend on the server/proxy; uploads are streamed, but the prototype uses a 120-second HTTP timeout and has no resumable upload support.

Contract tests simulate Artifactory's deploy, storage-info, and copy APIs, including failures and native Qt-generated releases. They are not a live Artifactory acceptance test. Before production, configure a test repository pair and run a small release and update through Actions, verify direct public download URLs, exercise a failed promotion, and confirm only the designated writer can replace heads. The [offline bundle workflow](offline-bundles.md) now publishes verified ZIPs and browser pages through this backend; configure its repository pair, token, and public HTML rendering.

For local workflow linting, actionlint 1.7.12 does not yet recognize GitHub's documented `concurrency.queue` property. Validate the `queue: max` entries against the GitHub documentation and use `actionlint -ignore 'unexpected key "queue" for "concurrency" section' .github/workflows/*.yml` for its remaining checks. [Upstream parser issue](https://github.com/rhysd/actionlint/issues/746).
