# Publish offline bundles and browser downloads

[`offline-bundles.yml`](../.github/workflows/offline-bundles.yml) assembles and publishes complete platform bundles to Cloudflare R2/S3 or Artifactory. It runs after `package-published` events, every six hours, or manually. Destinations come from `deployment/bundle-publications/*.json`; publishers come from the TUF-authenticated registry. [TUF configuration and archival policy](tuf.md) are required for CI publication.

Each destination represents one platform/season/channel. The job selects the latest compatible publisher revisions and includes only that selected platform set. Publishers and the registry authority may use different hosts from the downloads. The assembler needs its bundle key, not publisher or authority private keys.

## Publication layout and guarantees

```text
<public_url>/
  index.html                         # the only mutable download entry point
  bundles/<zip-sha256>/
    <bundle-id>.zip                   # complete offline package set
    bundle.json                      # signed details and pinned registry
    index.html                       # permanent page for this bundle
```

Preparation verifies the ZIP's signatures, exact member set, file hashes, dependency compatibility, and pinned registry. Native validation must then pass for the exact candidate and previous successful ZIP before publication uploads/promotes all three immutable files and verifies the origin's checksums/lengths. It checks anonymous public URLs: ZIP length through HEAD, exact metadata/page bytes through GET, and inline HTML rendering.

Only then does it replace the root `index.html`. This single write selects the new bundle. The page uses static HTML without executable JavaScript or external assets. It shows the platform, package versions, ZIP checksum/size, registry revision, offline authorization (no expiration date for v2), compatibility holdbacks, permanent links, and a link to the previous successful bundle.

There is no independently mutable public `latest.json`: separate page and pointer writes could disagree after a failure. The root page embeds an inert signed publication record that CI verifies before advancing it. The permanent `bundle.json` supplies machine-readable details. Local `assemble` still creates its existing local draft `latest.json`; it is not uploaded as a public head.

## Configure GitHub Actions

1. Copy the [R2 example](bundle-publications/linux-x64-r2.json.example) or [Artifactory example](bundle-publications/linux-x64-artifactory.json.example) to a `.json` file in that directory and replace placeholders. Repeat for other supported targets, with distinct download prefixes.
2. Set `source` to the reviewed authority bootstrap for that target. Set `storage.public_url` to the anonymous public root actually serving the configured bucket/repository prefix.
3. Configure the `offline-bundles` Actions environment, restrict it to trusted workflow refs, and install the applicable secrets below.
4. Set repository variable `BUNDLE_ASSEMBLY_ENABLED=true`.
5. Manually run **Publish offline bundles**, select a configuration filename if desired, and set `initialize=true` for first publication. Scheduled/event runs subsequently require an existing recognized page.

| Secret | Purpose |
| --- | --- |
| `BUNDLE_SIGNING_KEY` | Complete multiline Ed25519 PEM authorized by the registry |
| `BUNDLE_S3_ACCESS_KEY_ID`, `BUNDLE_S3_SECRET_ACCESS_KEY` | R2/S3 publication credentials |
| `BUNDLE_S3_SESSION_TOKEN` | Optional temporary AWS session token |
| `BUNDLE_ARTIFACTORY_ACCESS_TOKEN` | Artifactory publication credentials |

No production destinations or secrets are supplied. An existing manually uploaded HTML page is not adopted automatically: use a new prefix or plan a deliberate migration. Initialization permits an absent page; it does not bypass verification of an existing one.

The workflow uses three separate commands/jobs. The native command runs on the candidate's target platform, with Qt IFW bootstrapped beforehand:

```sh
python -m installer_tooling.ci_publication bundle-prepare --config deployment/bundle-publications/linux-x64.json --cache build/cache --candidate build/candidate --output build/preparation.json --initialize
python -m installer_tooling.offline_acceptance check --candidate build/candidate --source deployment/registries/linux-x64.json --ifw .tools/ifw --work build/native --report build/acceptance.json
python -m installer_tooling.ci_publication bundle-publish --config deployment/bundle-publications/linux-x64.json --candidate build/candidate --acceptance build/acceptance.json --output build/publication.json
```

Remove `--initialize` after first publication. Candidate/work directories must be new. The old one-step `bundle` publication command has been removed. The workflow retains the candidate and baseline ZIPs in an uncompressed Actions artifact for three days to transfer the exact bytes between jobs; native reports/logs and the final publication audit are also retained. Provision sufficient disk and artifact quota for two ZIPs, their extracted repositories, installed payloads, and temporary verification copies. No signing keys are included in artifacts.

## Required native acceptance

The workflow requires **all** `platform-checks` jobs and **all** selected `validate` jobs before any `publish` job runs. It uses normal successful-job dependencies, without `continue-on-error` or an `always()` publication condition. Failed, canceled, skipped, missing, or timed-out native checks block publication.

| Target | Native runner | Network restriction during installation |
| --- | --- | --- |
| Linux x64 | `ubuntu-24.04` | A new network namespace; hosted CI uses sudo to create it, then drops back to the runner user |
| Windows x64 | `windows-2022` | Temporary outbound Windows Firewall rules for the Python interpreter, Qt installer, and maintenance executables; removed in `finally` |
| macOS x64 | `macos-15-intel` | `sandbox-exec` with network access denied |
| macOS ARM64 | `macos-15` | Same native sandbox restriction |
| Linux/Windows ARM64 | Not configured | Discovery rejects publication until pinned native IFW builds and matching gates are added |

Runner labels are explicit, and the checker verifies the actual host OS/architecture against the signed target. The separate fixture matrix exercises Windows, Linux, and both macOS architectures on every publication run, even when only one destination is selected. [GitHub runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

The fixture gate installs signed revision 1 offline and upgrades a real component to revision 2 offline. The candidate gate performs these checks using the exact assembled ZIP:

1. Fresh installation of every offered component; verify installed versions and owned files.
2. Fresh installation of each registry-defined profile, including transitive dependencies, with an independent empty cache for every operation.
3. Install every component from the previous successful public bundle, using its original signed registry. Missing or corrupt baseline media fails the gate. Age alone does not invalidate v2 media; bounded v1 media must still be unexpired.
4. Run its maintenance tool against the candidate, then add newly offered packages. Verify versions and compare candidate-owned file hashes with the fresh install; preserve unrelated retained packages.
5. Offer the older bundle again and verify that inventory and candidate files do not downgrade.

For a genuinely first publication, there is no previous public ZIP: the candidate test explicitly reports `initial-reinstall` and tests idempotent maintenance against the same bundle. It cannot claim a historical upgrade. The required cross-platform fixture gate still tests a real version advance. An unchanged selection similarly exercises maintenance without inventing an older production version.

The checker fails if the isolation command fails or outbound TCP remains usable. Windows Firewall must be enabled, and adding/removing its scoped rules requires administrator rights. These rules protect Python and Qt; they do not confine arbitrary child tools introduced by future setup recipes. Linux/macOS restrictions apply to the worker's process tree. These are disposable-runner tests, not an end-user firewall configuration. [Windows program-scoped firewall rules](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule).

Native jobs receive no signing or storage secrets. Preparation signs a plan binding candidate/baseline records, destination, and the original live-page hash, but writes nothing remotely. The report binds the plan digest, exact ZIP hashes, target, and suite version. Publication requires a passing matching report, verifies the archived contents again, and checks that online authorization has not expired. Reports are evidence from the trusted same-run Actions jobs, not separately signed remote attestations; protect workflow/config changes and the publication environment accordingly.

The publishing job reacquires the destination lock and compares the current page hash with the signed preparation snapshot before uploading. If another run published during native validation, this run fails without changing public files; a later reconciliation prepares and tests against the new baseline. The final write still uses S3 conditional updates or the Artifactory designated writer. No package selection or assembly runs after acceptance.

## R2/S3 hosting

Use a public download bucket/custom domain and scoped credentials. For AWS, allow `s3:GetObject`, `s3:PutObject`, `s3:AbortMultipartUpload`, and enough listing permission to distinguish missing objects from denied access. Configure cleanup of abandoned multipart uploads to cover runner termination.

Files at least 64 MiB use bounded-memory multipart uploads and per-part Content-MD5 validation. CompleteMultipartUpload carries `If-None-Match: *` so it cannot replace an existing immutable object. Small uploads also send Content-MD5. The job checks SHA-256 metadata and object length after upload; the signed download record binds the entire ZIP's SHA-256. The final live-page update retains its previous ETag condition. [S3 completion API](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/complete_multipart_upload.html), [R2 compatibility](https://developers.cloudflare.com/r2/api/s3/api/).

HTML, ZIP, and JSON receive their appropriate content types. Immutable objects have a one-year immutable cache policy; the root page has `no-cache,max-age=0`. Configure the CDN to respect those headers and avoid long negative caching. Link explicitly to `<public_url>/index.html`; automatic directory-index routing is not assumed.

## Artifactory hosting

Use a dedicated Generic Local staging/release pair, such as `offline-bundles-staging-local` and `offline-bundles-release-local`. The backend stages, verifies, and promotes the exact `bundles/<sha256>/...` files using Copy Item, then replaces only the root `index.html` after verification.

Set `writer_repository` to the installer repository and keep its token exclusively in that repository's protected environment. Preparation and publication separately acquire the destination-based Actions lock; the signed baseline check prevents a stale candidate from committing after the intervening native jobs. GitHub concurrency is repository-scoped; all writers must route through this designated workflow. The backend checks that writer identity and re-reads the page immediately before replacement. See [Artifactory setup](artifactory.md).

Public HTML must render inline and ZIPs must download anonymously. Configure **Allow Content Browsing** where supported, or use a download frontend/proxy serving these same files. No script execution or weakening of script-security settings is needed. Publication rejects attachment-only HTML or unexpected public content before switching the live page. Confirm actual browser downloads work with the deployed content-security policy. [JFrog HTML browsing and script restrictions](https://jfrog.com/help/r/artifactory-blocked-script-execution).

Configure Artifactory/proxy caching to revalidate the root page and preserve immutable files. The helper does not change server security/cache settings. Upload limits depend on the server/proxy; Artifactory operations stream data but have a 120-second HTTP timeout and no resumable upload support.

## Failure, retry, and retention

- Assembly, archive verification, native installation/upgrade, upload, promotion, and public-routing failures leave the root page unchanged. Native failures happen before any public upload. Partial new files after publication failures may remain at unreferenced paths; previous ZIPs and permanent pages are retained.
- A failed final write before commit leaves the old page. A lost acknowledgement after commit is recovered by reading the origin: the exact intended page is recognized as success. If read-back is unavailable, the outcome is indeterminate. Inspect it before retrying; the job never performs an unconditional rollback over a potentially newer publication. Any committed candidate points to a verified complete bundle and retains the previous bundle link.
- Unchanged selections verify existing files/routing without reuploading the ZIP or replacing the page. Retries reuse identical immutable bytes; conflicting bytes fail.
- Registry and common publisher revisions cannot decrease; changed contents at the same revision are rejected. An older candidate therefore preserves the last successful selection. Deliberate policy resets require a reviewed migration or new destination.
- New schema-3 bundles use `wpilib-archive-v2` with explicit indefinite authorization. They remain installable without renewal or internet access, using the matching trusted installer/bootstrap; online TUF expiry and rollback checks remain strict when reconnecting. Existing v1 bundles retain their bounded expiry and need the [one-time migration](tuf.md#earlier-bounded-bundles). No automatic deletion is implemented; future retention must protect current/previous selections and permanent-link lifetime commitments.
- Tests cover assembly/transfer failures, corruption, public routing, final-write failures, lost acknowledgements, concurrency, retry, multipart completion/abort, missing or mismatched acceptance reports, modified candidates, and stale baselines. Linux native gates passed locally; Windows/macOS gates require GitHub-hosted execution. Live credentials and deployment-specific browser acceptance remain operational setup.
