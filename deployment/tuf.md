# TUF repositories and offline archival trust

The prototype uses `python-tuf==7.0.1` and `securesystemslib[crypto]==1.6.0` for online trust. The registry authority and every publisher have independent TUF roots, keys, metadata, and publication destinations. Core, Elastic, AdvantageScope, RobotPy, and future publishers do not share a snapshot or timestamp job. The existing registry controls publisher admission, component ownership, profiles, and compatibility policy.

## Repository layout

Each publisher or authority destination contains:

```text
releases/N/...                       # publisher's immutable Qt files / archival release.json
revisions/N.json                     # authority's immutable archival registry record
tuf/
  metadata/
    1.root.json                      # keep every sequential root version
    N.targets.json
    N.snapshot.json
    N.timestamp.json                 # immutable audit copy
    timestamp.json                   # only mutable online commit point
  targets/
    <sha256>.catalog.json
```

The logical TUF target is `catalog.json`, downloaded using TUF's hash-prefixed target naming. A publisher catalog contains up to eight complete signed release manifests, in increasing revision order. Each manifest binds every IFW metadata/archive hash and length. The authority catalog is its signed archival registry record. Thus python-tuf authenticates the catalog; the adapter verifies Qt files against the hashes transitively bound by that catalog. Qt never handles remote TUF metadata itself.

The old mutable `index.json` / `registry.json` are not online discovery heads in TUF mode. Local fixture generation may still emit them for debugging. CI publishes only the TUF head. No old payload archives are needed for release generation or TUF renewal; the origin's catalog and metadata are sufficient.

The client uses `ngclient.Updater`, including normal current-clock expiration, signature thresholds, rollback checks, consistent metadata, and sequential root updates. Per-repository metadata is kept under a locked persistent cache. Registry transport failures may use a cached catalog only after another normal TUF verification; signature/hash/expiry/rollback failures never use fallback. Publisher selection and bundle assembly require fresh online TUF metadata. [Python TUF client](https://theupdateframework.readthedocs.io/en/stable/api/tuf.ngclient.html).

## Separate online and archival authority

Online registry authenticity comes exclusively from the authority's TUF chain and authenticated catalog bytes. The client then validates the registry identity, season/channel/platform, schema, increasing revision, issuance, and finite online expiry. It does not check that catalog's archival signature against the installer's old `public_key`, or require the current archival policy to match the old offline bootstrap. A TUF-authorized archival-key or policy change therefore cannot strand an old installer online. The catalog still carries the archival envelope for bundle assembly.

Offline mode continues to verify that envelope against the independent archival `public_key` and policy pinned in its matching installer/bootstrap. Online verification never imports a replacement offline pin. Old bundles retain their original trust; a new bundle signed with a replacement authority key requires a bootstrap explicitly trusting that key. The CLI bundle command and CI preparation enforce this additional offline authorization before assembly. They cannot turn a merely TUF-authenticated catalog into offline authorization for an unrelated key.

The registry cache identity now uses authority TUF identity/original root and registry scope, excluding the archival key and endpoint. Each cached record includes the exact catalog and timestamp/snapshot/targets bytes, plus the root version. Historical verification replays the saved root chain from the original bootstrap and verifies role thresholds, metadata versions, and every hash/length link. Expired evidence establishes only the previously observed registry revision; it can never authorize an online plan or fallback. All usable catalogs still pass normal current-clock `Updater.refresh()`. The old key-namespaced cache is migrated once by verifying its historical floor against its original archival pin; it is never treated as a fresh online response.

To rotate `REGISTRY_SIGNING_KEY`, replace that Actions secret and run authority renewal using the existing TUF configuration. The helper verifies the previous catalog through TUF, detects the changed archival signer, and publishes a higher registry revision even if its online expiry is not near. No old archival private key or old installer's bootstrap edit is required for online updates. For new offline media, distribute a matching archival bootstrap and use a separate download publication scope when the old page/baseline was signed under a different archival pin. Existing media and its original bootstrap remain unchanged.

## Initial keys and configuration

On a trusted administrative machine, install the project's dependencies and run once for the authority and once for each independent publisher scope:

```sh
python tools/tuf_admin.py init --repository-id elastic-2027-stable-linux-x64 --url https://downloads.example.org/elastic/2027/stable/linux-x64/tuf --output /secure/elastic-tuf
```

This creates three root keys with a two-signature threshold, one distinct key for each online role, `1.root.json`, and `trust.json`. It refuses an existing output directory. Store the root keys separately offline with recovery copies; they do not go into Actions. Demo keys use a single root signer and must never be deployed.

Embed the complete generated `trust.json` object as `tuf` in the publisher's approved registry entry. Embed the authority's corresponding object as `tuf` in the installed registry-source bootstrap. Each object has `repository_id`, `url`, and a complete signed `root` object. Pin it through reviewed configuration, never from a downloaded unsigned URL. Keep that original bootstrap stable during ordinary root rotation; new roots are fetched through their authenticated sequence.

For offline verification and bundle publication, the authority bootstrap and approved registry must contain the same archival policy:

```json
{"archive_policy": {"name": "wpilib-archive-v2", "lifetime": "indefinite"}}
```

Set `online_trust` to `tuf` in the approved registry. Every listed publisher must then have valid TUF trust. Keep the bootstrap's existing authority `public_key`, each publisher's `public_key`, and `bundle_public_key`: these are **separate archival signing keys**, not substitutes for TUF keys. They authorize retained offline media. The existing scope fields and namespace allowlists remain required.

For CI, `tuf.url` must equal the corresponding `storage.public_url` plus `/tuf`. The authority bootstrap's legacy `url` remains `<storage.public_url>/registry.json` as its stable configuration identity; TUF mode does not fetch that URL. Public delivery remains ordinary HTTPS files on R2 or Artifactory Generic Local repositories.

## CI secrets and renewal

| Owner | Secrets | Use |
| --- | --- | --- |
| Each publisher | `TUF_TARGETS_KEY`, `TUF_SNAPSHOT_KEY`, `TUF_TIMESTAMP_KEY` | Its independent online TUF roles, one full PEM per secret |
| Each publisher | `PUBLISHER_SIGNING_KEY` | Archival release manifests; required for releases, absent from renewal jobs |
| Authority | `REGISTRY_TUF_TARGETS_KEY`, `REGISTRY_TUF_SNAPSHOT_KEY`, `REGISTRY_TUF_TIMESTAMP_KEY` | Authority online roles; workflow maps these to the helper's `TUF_*_KEY` variables |
| Authority | `REGISTRY_SIGNING_KEY` | Explicit archival registry authorization |
| Bundle assembler | `BUNDLE_SIGNING_KEY` | Exact offline contents and candidate plan |

All of these are distinct keys. The existing backend-specific storage secrets remain unchanged. CI removes PEM values from child environments, writes temporary mode-0600 files, and cleans them up. Root thresholds are supported by the library and administration tool; shipped Actions mappings use one online key per role. An online role configured for a higher threshold needs a corresponding multi-signer workflow/service before publication can succeed.

[`publisher-release.yml`](../.github/workflows/publisher-release.yml) signs and verifies publisher payloads, publishes immutable release files, creates the catalog and all three online roles, uploads/promotes and verifies their immutable files, and updates `tuf/metadata/timestamp.json` last. S3/R2 keeps conditional writes; Artifactory uses the same designated writer and destination concurrency group. Failed promotion leaves the prior timestamp intact. Orphaned metadata version numbers are skipped; immutable data is never replaced.

Default lifetimes are timestamp **2 days**, snapshot **7 days**, targets **30 days**, capped by root expiry. Every renewal re-signs all three roles with increased metadata versions while preserving the exact catalog. A daily schedule is required; monitor failures and expiry independently because scheduled Actions can stop running. Online keys are a deliberate unattended-CI tradeoff: compromise of an authorized signing workflow remains a compromise of its publishing authority.

Each producer owns a scheduled caller of [`publisher-renewal.yml`](../.github/workflows/publisher-renewal.yml):

```yaml
name: Renew installer repository
on:
  schedule:
    - cron: '21 4 * * *'
  workflow_dispatch:
permissions:
  contents: read
jobs:
  renew:
    uses: OWNER/INSTALLER_REPO/.github/workflows/publisher-renewal.yml@FULL_40_CHARACTER_COMMIT_SHA
    with:
      tooling_repository: OWNER/INSTALLER_REPO
      tooling_ref: FULL_40_CHARACTER_COMMIT_SHA
      publication_config: deployment/publisher-publications/elastic-linux-x64.json
    secrets:
      TUF_TARGETS_KEY: ${{ secrets.TUF_TARGETS_KEY }}
      TUF_SNAPSHOT_KEY: ${{ secrets.TUF_SNAPSHOT_KEY }}
      TUF_TIMESTAMP_KEY: ${{ secrets.TUF_TIMESTAMP_KEY }}
      S3_ACCESS_KEY_ID: ${{ secrets.S3_ACCESS_KEY_ID }}
      S3_SECRET_ACCESS_KEY: ${{ secrets.S3_SECRET_ACCESS_KEY }}
      # For Artifactory, replace the S3 mappings with ARTIFACTORY_ACCESS_TOKEN.
```

The renewal wrapper uses the same publisher environment, destination identity, and serialized writer as releases. It downloads no release artifact and does not invoke repogen. Authority [`registry-renewal.yml`](../.github/workflows/registry-renewal.yml) renews TUF on every successful run; it creates a new registry record only when its online policy lifetime approaches expiry or an approved configuration is applied. Routine renewal never changes existing bundles or their archival authorization. Omit `offline_days` from publication configuration under the indefinite policy; specifying it is an error. The online `valid_days` setting remains seven days by default.

Origin metadata is verified by signatures and hash links even if expired so CI can recover an expired repository. This is a publishing operation, never the client verification path. The private authenticated publication origin is trusted operational state: protect it against unauthorized overwrites/replay, retain storage version history and publication audits, and keep deployment credentials in the single designated writer. TUF protects clients against an untrusted delivery mirror; it cannot make a compromised authorized publisher's new signatures trustworthy.

## Root rotation

`tools/tuf_admin.py rotate --previous 1.root.json --keys rotation-keys.json --output 2.root.json` produces the next root. Its keys file maps `root`, `targets`, `snapshot`, and `timestamp` to PEM path lists and `old_root` to the old root signers. Both old and new root thresholds must verify. The default new root threshold is two.

Publish reviewed root files as immutable `tuf/metadata/N.root.json` through an administrative process under the same destination lock. Retain every intermediate root. Root-only key rotation can keep online role keys unchanged. To rotate online role keys without interrupting renewal, first publish a root authorizing both old and new online keys, switch CI secrets and successfully renew metadata, then publish another root removing the old online keys. Do not remove a key before the current metadata has been re-signed by its replacement. Clients follow the chain automatically. Emergency recovery after a compromised threshold requires a separately reviewed recovery process; it is not automated by daily renewal. [TUF key management](https://theupdateframework.github.io/specification/latest/#key-management-and-migration).

### Catching up after years offline

An expired bundled root and expired intermediate roots may authenticate subsequent root versions. The final root and update metadata must be unexpired. Every transition advances exactly one version and meets both the old and new root thresholds. There is no jump to an unsigned current root or change to the system clock. [TUF root update workflow](https://theupdateframework.github.io/specification/latest/#update-the-root-role).

The client supports histories longer than python-tuf's default 256 rotations. It performs up to four batches of 256 remote root requests per operation. Each batch uses a fresh standard Updater with the **original pinned bootstrap**, revalidating all cached intermediate signatures. Cached file names increase the traversal allowance but never establish trust. A fetcher budget limits new remote requests and refuses timestamp/target access at a batch boundary: reaching a work limit is not interpreted as reaching the end of the root chain.

If more work remains after those batches, the operation raises `RootUpdatePending`. `refresh-registry` and `check-updates` report `status: root-update-pending` and `retryable: true`, with nonzero exit status. Retry using the same cache to continue; validated root files are atomically persisted by python-tuf, even when a process exits before completion. No catalog, installation, or fallback is authorized by a partial catch-up. Larger lifetime histories do not require raising a fixed cumulative limit. Current root/file size limits, download timeouts, signature/hash checks, rollback checks, and final expiry checks remain in place. Preserve the cache across any CI retries that encounter this status.

The publisher-side origin verifier also traverses the complete sequential root history, without its former 256-version limit, and verifies every transition and final root expiry before publishing or renewing. It reads the authenticated publication origin and does not update a client cache. Retain every root transition on R2/Artifactory and keep the authority endpoint reachable for old installers.

## Explicit offline policy: wpilib-archive-v2

Offline mode has its own authorization path. It never asks python-tuf to accept an expired timestamp, changes the clock, or disables TUF expiry.

1. Assembly must have freshly verified the authority and every selected publisher with TUF. The bundle signer attests the exact selected manifests and records stable TUF repository identities and catalog hashes as provenance; metadata-only renewal therefore does not force a new ZIP. Those records are not a self-contained replay of the online verification chain.
2. The trusted bootstrap and authority-signed registry must explicitly agree on `{"name":"wpilib-archive-v2","lifetime":"indefinite"}`. The record authorizes publisher archival keys, the bundle key, and namespaces. Its `offline_expires_at` must be explicitly `null`; an absent field or a numeric expiry is rejected. Its positive `issued_at` records provenance, while the separate numeric `expires_at` still limits online registry use.
3. A schema-3 bundle includes the exact authority record and a signed `archival` section with `policy: wpilib-archive-v2`, matching `issued_at`, explicit `expires_at: null`, and publisher TUF provenance. There is no offline validity window or wall-clock check. Repeated assembly from the same inputs is deterministic.
4. Offline verification checks the bootstrap authority signature, explicit policy, bundle signature, pinned registry identity, publisher signatures, every payload hash/length, namespaces, ownership, and dependencies. No repository access, renewed signatures, accurate clock, or online activation is required. Missing files, tampering, unknown policies, and inconsistent authorization still fail closed even with a warm download cache.
5. Offline access never writes TUF metadata or lowers online version history. An installation made offline can subsequently refresh TUF and update online normally. The manager retains its no-package-downgrade rule.

An old authorized bundle remains installable indefinitely, including after online roots, targets, snapshots, timestamps, and registry authorization expire or a later registry removes a publisher. Preserve the matching trusted installer/bootstrap alongside the ZIP. Ordinary online renewal and root rotation do not revoke that offline authorization. A deliberately replaced bootstrap may change trust or raise its minimum revision; it does not rewrite the installer already on disconnected media. Package downgrade protection still applies to an existing newer installation.

A disconnected machine cannot learn later revocations or distinguish a genuinely old signature from a backdated forgery made after archival-key compromise. Non-expiring archival trust accepts that limitation; it makes no claim of current approval or current online freshness. Protect archival signing keys accordingly. TUF's current-clock expiry and rollback rules remain mandatory for every subsequent online check and upgrade. Archival authority-key replacement requires an explicitly trusted bootstrap for new offline media; existing clients continue updating online through TUF without adopting the replacement offline key.

Native publication gates still apply to every exact ZIP and its last successful upgrade baseline. Missing or corrupt baseline media blocks publication; age alone does not invalidate a v2 baseline. Old offline downloads remain retained; renewal does not rewrite them.

### Earlier bounded bundles

`wpilib-archive-v1` remains supported only with its matching bounded bootstrap policy (`max_days`, previously 365 by default). Those signed records and bundles still expire at their original deadline; existing installed software is unaffected. Removing expiry from a signed file would invalidate its signature, and this implementation does not reinterpret v1 as indefinite.

For the one-time policy migration, create a v2 bootstrap and matching approved registry, omit `offline_days`, initialize a new authority/bundle publication scope, and regenerate the offline bundles with fresh signatures and native acceptance checks. Existing publisher payloads and repositories can be reused. Distribute the v2-capable installer/bootstrap together with the regenerated bundles. Use a new client cache for the new bootstrap scope. Old binaries cannot be made to understand the new policy without updating them. After this migration, retained v2 bundles require no periodic renewal or reconnection.

## Migration and validation

Existing deployed metadata is not automatically adopted as a TUF repository. Create reviewed roots and archival policy, add the new secrets and daily producer renewal callers, initialize the authority TUF catalog explicitly, then initialize each publisher's TUF catalog from a complete release. Use a new scope/prefix when needed to avoid reserved release IDs. Existing bootstrap executables need this one-time protocol update; subsequent publisher additions remain registry-driven.

The CLI demo and native publication fixtures now use TUF. Legacy local/loopback fixtures remain for regression tests, but HTTPS registry refresh and every CI publication command require TUF configuration. No silent downgrade from a TUF error to the custom online protocol is implemented.

Tests exercise the real Python TUF updater over files and HTTP, metadata expiry/rollback/corruption, root rotation and thresholds, verified cache fallback, independent publisher keys, Artifactory timestamp-last promotion, publisher and registry CI helpers, and native offline installation with archival verification evaluated in the year 2100 followed by an online upgrade after archival-authority rotation. Continuity tests cover 520 root versions with expired intermediates and two-of-three key rotation, resuming a 1,030-version history, broken chains, expired final roots, tampered caches, historical TUF evidence, and retained offline pins. Windows/macOS runners and live cloud publication still require execution in the deployment environment.
