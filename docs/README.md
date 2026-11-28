# Documentation

- [Client](client.md): application structure, installation/maintenance operations and acceptance workflows.
- [Architecture](architecture.md): client/worker responsibilities, publication, online trust and offline media.
- [Production readiness](production-readiness.md): implemented capabilities and remaining release gates.
- [Development](../CONTRIBUTING.md): dependencies, build commands and CI responsibilities.
- [Self-contained runtime](self-contained-runtime.md): dependency isolation, supported baselines and acceptance limits.
- [Package catalog review draft](package-catalog-review.md): components, dependencies, profiles, ownership and open decisions.
- [Package catalog development](package-catalog.md): legacy/Maven imports and producer CI recipes.
- [Real package recipes](../packaging/README.md): pinned Elastic and AdvantageScope releases.
- [Deployment](../deployment/): signing, TUF, Artifactory/R2, registries and offline publication.

Acceptance workflows publish reports, logs, hashes and screenshots as CI artifacts. Release approval must refer to the exact client, package set and trust configuration tested.
