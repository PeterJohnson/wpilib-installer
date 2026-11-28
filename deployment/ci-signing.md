# Signing and GitHub Actions publication

The prototype implements the following deployable workflows:

- [`publisher-release.yml`](../.github/workflows/publisher-release.yml) is reusable by core, Elastic, AdvantageScope, RobotPy, or another authorized publisher. Each caller retains its own signing key and storage credentials.
- [`publisher-renewal.yml`](../.github/workflows/publisher-renewal.yml) is called daily from each producer to renew its own TUF roles without rebuilding payloads.
- [`registry-renewal.yml`](../.github/workflows/registry-renewal.yml) runs in the registry authority repository, with daily renewal and explicit manual publication of reviewed changes.

All support Cloudflare R2/S3 (`storage.type: s3`, the backward-compatible default) and Artifactory (`storage.type: artifactory`). Install CI dependencies with `pip install '.[ci]'`. Clients still fetch ordinary public HTTPS files, and different publishers can use different backends. The API endpoint is for CI only. No cloud account, credentials, remote repository, or live deployment is configured by checking in these workflows. See the [Artifactory staging guide](artifactory.md) for repository setup, promotion behavior, and the required writer isolation.

The offline workflow, [`offline-bundles.yml`](../.github/workflows/offline-bundles.yml), publishes complete ZIPs and browser pages. See [offline download deployment](offline-bundles.md) for configuration, storage secrets, and preservation of the last successful selection.

## Keys and trust bootstrap

First create independent TUF roots and role keys using [the TUF setup guide](tuf.md). Root private keys remain offline. The keys below are additional archival signing keys; all release/registry jobs now require the three online TUF role secrets as well.

Generate a distinct Ed25519 key for each publisher, one for the registry authority, and one for the bundle assembler. On a trusted administrative machine, this command writes a mode-0600 PKCS#8 PEM file and prints only the public key:

```sh
python -c 'from installer_tooling.common import keygen; print(keygen("/secure/location/registry.pem"))'
```

Repeat with distinct paths for the other roles. Keep recovery copies outside GitHub; do not use demo keys. Git ignores PEM files, but that is not a substitute for keeping them outside the checkout and artifacts. Place the authority public key in the trusted `deployment/registries/<target>.json` bootstrap. Place each publisher public key and the bundle public key in the approved registry configuration. PEM private keys go into Actions secrets as their complete multiline text; do not base64-wrap them.

| Repository/environment | Secret | Value |
| --- | --- | --- |
| Each producer / `publisher-release` | `TUF_TARGETS_KEY`, `TUF_SNAPSHOT_KEY`, `TUF_TIMESTAMP_KEY` | Separate online TUF role PEMs; used for releases and daily renewal |
| Each producer / `publisher-release` | `PUBLISHER_SIGNING_KEY` | That publisher's Ed25519 PEM |
| Each producer / `publisher-release` | `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Credentials for only that publisher's storage |
| Each producer / `publisher-release` | `S3_SESSION_TOKEN` | Optional temporary AWS session token |
| Each producer / `publisher-release` | `ARTIFACTORY_ACCESS_TOKEN` | Publisher-scoped Artifactory access token, when using Artifactory |
| Registry authority / `registry-authority` | `REGISTRY_TUF_TARGETS_KEY`, `REGISTRY_TUF_SNAPSHOT_KEY`, `REGISTRY_TUF_TIMESTAMP_KEY` | Authority online TUF role PEMs |
| Registry authority / `registry-authority` | `REGISTRY_SIGNING_KEY` | Authority Ed25519 PEM |
| Registry authority / `registry-authority` | `REGISTRY_S3_ACCESS_KEY_ID`, `REGISTRY_S3_SECRET_ACCESS_KEY` | Credentials for registry publication storage only |
| Registry authority / `registry-authority` | `REGISTRY_S3_SESSION_TOKEN` | Optional temporary AWS session token |
| Registry authority / `registry-authority` | `REGISTRY_ARTIFACTORY_ACCESS_TOKEN` | Authority-scoped Artifactory access token, when using Artifactory |
| Offline assembly repository | `BUNDLE_SIGNING_KEY` | Bundle Ed25519 PEM, already used by the assembly workflow |

For example, `gh secret set REGISTRY_SIGNING_KEY --env registry-authority < /secure/location/registry.pem` installs the authority key without placing it in shell arguments. Configure environments before enabling jobs. Restrict deployment branches/tags to trusted release refs and protect workflow and configuration changes. The publisher workflow excludes PR, PR-target, and workflow-run events. These checks complement repository permissions; they do not make arbitrary trusted-branch code safe. Routine signing can run without environment reviewer prompts when branch protections provide the intended approval policy.

For AWS S3, grant `GetObject` and `PutObject` on the required prefix and sufficient `ListBucket` permission to distinguish an absent key from denied access. The helper does not list or delete objects. For ordinary R2 API tokens, use separate publisher and authority buckets: documented token scoping is by bucket, so a shared bucket does not isolate publishers just because their configured prefixes differ. [R2 credentials](https://developers.cloudflare.com/r2/api/tokens/).

Supply only the credentials for the selected storage backend. Artifactory additionally requires a configured authoritative GitHub writer repository and the workflow's destination-based serialization key. Signing keys retain the same separation regardless of the storage backend.

## Storage and configuration

Copy the checked-in `.json.example` files to `.json` after replacing placeholders:

- [`publisher-publications/elastic-linux-x64.json.example`](publisher-publications/elastic-linux-x64.json.example), in each producer's repository, selects the publisher, a trusted bootstrap path, and the destination storage.
- [`registry-publications/linux-x64.json.example`](registry-publications/linux-x64.json.example), in the authority repository, selects the bootstrap, reviewed editable registry, storage, and validity policy.
- Artifactory alternatives: [publisher configuration](publisher-publications/elastic-artifactory-linux-x64.json.example) and [authority configuration](registry-publications/artifactory-linux-x64.json.example). Choose one configuration per destination, not both examples for the same feed.

All referenced file paths are relative to the corresponding repository root. The publisher bootstrap is checked-in trusted configuration, never taken from the input artifact. The actual approved registry JSON follows the [registry contract](registries/README.md); it is not the signed envelope. Add analogous configuration files for the required platforms and seasons/channels. The renewal matrix reads every `deployment/registry-publications/*.json`, so it has no publisher or platform list compiled into the job.

`storage.prefix` is the exact repository root within the bucket or Artifactory repository, without a leading or trailing slash. `storage.public_url` must map that prefix to the public HTTPS root. For a publisher it must equal the URL authorized by the registry. For the authority, appending `/registry.json` must equal the bootstrap URL. These checks validate declared mappings; operators must configure the host/CDN to serve those actual object keys. For AWS, omit `endpoint_url` and set the appropriate region. For R2, use the account's HTTPS S3 API endpoint and region `auto`. For Artifactory, set the direct `api_url`, distinct staging/release repositories, and `writer_repository` as described in its guide.

The S3/R2 origin must support atomic `If-None-Match: *` creation and `If-Match: <etag>` replacement. This backend sets mutable heads to `Cache-Control: no-cache,max-age=0` and immutable objects to a one-year immutable cache policy. Configure the CDN to respect those headers and avoid long negative caching of newly created objects. S3/R2 now uses bounded-memory multipart uploads for files at least 64 MiB, with the immutable write condition on completion and cleanup on failure. Small uploads and individual parts send Content-MD5; completed download objects are checked against stored SHA-256 metadata and length. Artifactory uses serialized promotion instead, with head-cache rules configured on the server/proxy; it does not claim conditional-PUT support. [S3 PutObject conditions](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html), [R2 compatibility](https://developers.cloudflare.com/r2/api/s3/api/).

## Publisher release integration

The [catalog development plan](../docs/package-catalog.md) adds shared packaging recipes with two sources: pinned legacy/Maven release artifacts for development and local producer build outputs for eventual CI integration. Both emit the input contract below. The reusable publication workflow exists today; general SDK importers and producer-facing packaging actions remain implementation work.

The producer's build job must finish platform signing/notarization and tests before it hands off the payloads. The input directory has this shape:

```text
release-input/
  spec.json
  payloads/
    dashboard/...
```

`spec.json` uses the existing `publish` command's specification: publisher, target, season, channel, positive increasing revision, and packages with ID, display name, numeric IFW version, dependencies, and relative payload directory. Supply the complete current component set for this publisher. Independent publishers do not supply each other's payloads.

Create a tar file with clean relative entries (no leading `./`) and upload that file as the same-run artifact:

```yaml
# At the end of the existing successful build/test/sign job:
- name: Package publication input
  run: tar -cf release.tar -C release-input spec.json payloads
- uses: actions/upload-artifact@v4
  with:
    name: installer-linux-x64
    path: release.tar
    if-no-files-found: error
```

Tar preserves executable modes that Actions artifacts otherwise discard. Extraction preserves contained relative symlinks and rejects traversal, escaping/dangling/cyclic links, hardlinks, devices, duplicate entries, and writes below symlinks. macOS signature attributes need the dedicated preservation recipe documented in [real package recipes](../packaging/README.md); generic tar handoff alone does not establish signature preservation. [Artifact permission behavior](https://github.com/actions/upload-artifact#permission-loss).

Add a dependent job to that producer's trusted release workflow. Replace `OWNER/INSTALLER_REPO` and `FULL_40_CHARACTER_COMMIT_SHA` with the published tooling repository and the same reviewed commit in both locations:

```yaml
publish-installer:
  needs: build-test-sign
  permissions:
    contents: read
  uses: OWNER/INSTALLER_REPO/.github/workflows/publisher-release.yml@FULL_40_CHARACTER_COMMIT_SHA
  with:
    tooling_repository: OWNER/INSTALLER_REPO
    tooling_ref: FULL_40_CHARACTER_COMMIT_SHA
    publication_config: deployment/publisher-publications/elastic-linux-x64.json
    artifact_name: installer-linux-x64
    spec_path: spec.json
    initialize: false
  secrets:
    PUBLISHER_SIGNING_KEY: ${{ secrets.PUBLISHER_SIGNING_KEY }}
    TUF_TARGETS_KEY: ${{ secrets.TUF_TARGETS_KEY }}
    TUF_SNAPSHOT_KEY: ${{ secrets.TUF_SNAPSHOT_KEY }}
    TUF_TIMESTAMP_KEY: ${{ secrets.TUF_TIMESTAMP_KEY }}
    S3_ACCESS_KEY_ID: ${{ secrets.S3_ACCESS_KEY_ID }}
    S3_SECRET_ACCESS_KEY: ${{ secrets.S3_SECRET_ACCESS_KEY }}
    S3_SESSION_TOKEN: ${{ secrets.S3_SESSION_TOKEN }}
```

For Artifactory, select its configuration and replace the three S3 secret mappings with `ARTIFACTORY_ACCESS_TOKEN: ${{ secrets.ARTIFACTORY_ACCESS_TOKEN }}`. Keep all four signing-key mappings. The called job uses the caller's `publisher-release` environment. Pass the named secrets explicitly, including when they exist only in that environment. The tooling checkout is explicit because a reusable workflow's default checkout is the caller repository. The workflow downloads only an artifact from the same run, not an arbitrary PR or unrelated workflow. [Reusable workflow secret/environment behavior](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

Set `initialize: true` only for intentional first publication. A missing TUF timestamp otherwise fails. Choose a persistent increasing revision counter per publisher/target, generated by the producer workflow; do not reset it when renaming or replacing a workflow. A stable release workflow's `github.run_number` can provide a counter if that workflow is the sole allocator. `run_attempt` is not a release revision. Publication checks the origin head and rejects reused or older revisions.

The job holds `installer-publication-<destination identity>` across generation and publication. It verifies the origin TUF chain and catalog, builds this publisher's complete snapshot, publishes immutable payloads, and publishes a catalog retaining up to eight revisions. R2 uploads directly; Artifactory stages, verifies, and promotes the exact files. Both publish versioned TUF metadata and replace only `tuf/metadata/timestamp.json` last. Old objects remain untouched. Audit artifacts contain identities and metadata versions, never private keys. See [TUF publication and daily renewal](tuf.md).

There is no shared publication job and no cross-repository dispatch credential required. The existing six-hour bundle schedule detects new releases. Deployments may add `package-published` dispatch after a successful head update to reduce that delay; this workflow does not send one. Online updates are available independently of bundle assembly.

## Registry initialization, changes, and renewal

1. Add reviewed bootstrap, approved registry, and storage configuration files. The authority key must match each bootstrap using that workflow; separate authorities need separately scoped jobs/environments.
2. Configure the `registry-authority` secrets and set repository variable `REGISTRY_RENEWAL_ENABLED=true`.
3. Run **Renew TUF registries and archival authorization** manually with `mode=apply`, the desired configuration filename, and `initialize=true` for first publication. This signs the approved JSON. An existing head is still verified and never reset.
4. For a later reviewed publisher/profile/key/policy change, run `mode=apply`, normally with `initialize=false`. Keep the bootstrap unchanged when only the approved publisher list changes.
5. Let the daily scheduled `renew` run maintain online authorization. It reads the current signed registry from the storage origin, verifies it against the bootstrap, and republishes the same configuration when at most three days remain. Defaults are seven days online and indefinite offline authorization under `wpilib-archive-v2`. Omit `offline_days` from publication configuration for this policy. When the registry record is not due, the job still renews all three online TUF roles without changing that record.

Renewal derives the next revision from the verified head, rather than from GitHub's workflow counter or a checked-in number. It can recover after online expiry while retaining revision history. It skips already occupied immutable revision paths from interrupted publication, including Artifactory staging-only revisions; occupied revision numbers are skipped without adopting their contents. A manual `apply` is the only operation that reads `approved_registry`; the scheduled path cannot accidentally restore stale contents from that file.

New signed registry revisions are discovered by clients automatically. Existing bundles keep their old authority envelope, revision, hash, and archival policy. V2 bundles require no offline renewal; older v1 bundles keep their original expiry until regenerated under the [v2 migration procedure](tuf.md#earlier-bounded-bundles). Offline use never alters a client's cached online floor. The bundle job needs only its bundle key and the public bootstrap.

The online client and authority renewal helper authenticate the registry through TUF, independently of the archival `public_key` in an old bootstrap. To rotate the archival authority signer, replace `REGISTRY_SIGNING_KEY` and run renewal. A changed signer causes a new record revision even when renewal is not otherwise due; the three TUF role secrets authorize publication as usual. Existing online clients need no new archival pin. New offline bundles need a matching trusted bootstrap and a download scope whose page/upgrade baseline uses that pin. Preserve the old installer/bootstrap and media. See [separate online and archival authority](tuf.md#separate-online-and-archival-authority).

## Failure recovery and limits

- Failed payload uploads or promotions never advance a publisher's TUF timestamp. R2 uses conditional writes; Artifactory uses one authorized serialized writer and checks for an unexpected head change immediately before replacement. A timeout during the final head write can have an indeterminate outcome: inspect the live head before retrying. Rerun registry renewal to read the current head and skip abandoned revisions.
- Identical immutable bytes can be uploaded again, but different bytes at an existing path fail. Publisher retries may need a new revision because rebuilding an archive can change its bytes, and a staged signed manifest reserves its revision. Allocate a higher revision/new release run; never delete or overwrite the old revision to force a retry. Successful duplicate publisher revisions are also rejected explicitly.
- S3 accepts only `NoSuchKey` as absence. Artifactory checks both repositories exist and are readable before accepting individual artifact 404s. Permission errors, malformed metadata, signature failures, redirects, and transport errors fail closed. First initialization is explicit, not automatic error recovery.
- Monitor authority expiry and scheduled-job failures. Public repositories can have schedules disabled after inactivity; restore the job or manually renew before clients' authorization expires. [GitHub schedule behavior](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).
- The offline bundle workflow requires Windows, macOS, and Linux native offline lifecycle gates plus validation of each exact candidate on its matching host before publication. Preparation signs a frozen candidate without remote writes; the publish job requires a matching report and unchanged upgrade baseline. Native jobs receive no signing/storage secrets; only preparation receives the bundle key. See its deployment guide for artifact handoff, platform coverage, and live-host setup.
- Storage behavior is covered by S3 SDK and Artifactory REST contract tests, failure simulations, and native Qt publication tests. Smoke-test a real destination and its public URL mapping before live releases. The native fixture workflows run in GitHub Actions without production signing/storage secrets. This checkout has not published releases to AWS, R2, or Artifactory.
- TUF online verification, long root-history catch-up, and online continuity across archival-authority rotation are implemented. Metadata signing does not implement platform code signing, emergency recovery, distribution of replacement offline trust pins, withdrawal administration, or a signing service. Online CI keys and authoritative origin state remain trusted publishing infrastructure; protect them independently from the public delivery mirror.
