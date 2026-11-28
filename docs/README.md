# Documentation

## Current implementation and operations

- [C# manager experiment](csharp-manager-experiment.md): policy adapters, native AOT application checks, and distribution size comparison.
- [C# Native AOT TUF investigation](csharp-tuf-evaluation.md): candidate libraries, measured interoperability and remaining adapter work.
- [Architecture](architecture.md): selected Rust/private-Qt design, publication, trust, and offline media.
- [Production readiness](production-readiness.md): established capabilities and unfinished release gates.
- [Development and migration](../CONTRIBUTING.md): current layout, commands, and CI responsibilities.
- [Runtime distribution](self-contained-runtime.md): dependency isolation, supported baselines, measured sizes, and acceptance limits.
- [Package catalog review draft](package-catalog-review.md): proposed components, dependency graph, profiles, ownership and open decisions.
- [Package catalog development](package-catalog.md): legacy/Maven imports and shared producer CI recipes/actions.
- [Real package recipes](../packaging/README.md): pinned Elastic and AdvantageScope upgrades.
- [Deployment](../deployment/): signing, TUF, Artifactory/R2, registries, and offline publication.

## Evidence and history

Machine-readable reports in [evidence/](evidence/) bind test results to exact commits, executable/bundle/ISO hashes, and workflow runs. They are historical records and are not rewritten when source paths move.

- [Native TUF comparison](evidence/native-tuf-2026-11-27.json)
- [Single-application acceptance](evidence/rust-application-2026-11-27.json)
- [Online/cache acceptance](evidence/rust-online-2026-11-27.json)
- [Initial self-contained runtime](evidence/self-contained-2026-11-27.json)
- [Real dashboard clean Desktop acceptance](evidence/real-desktop-2026-11-27.json)
- [Real dashboard Desktop acceptance after the layout migration](evidence/real-desktop-production-layout-2026-11-27.json)
- [Production-layout client and tooling validation](evidence/production-layout-2026-11-27.json)
- [Host graphics and clean Desktop fixtures](evidence/host-graphics-2026-11-27.json)

The [original proposal](history/installer-architecture.md), [implementation comparison](history/manager-distribution-evaluation.md), [prototype status](history/prototype-status.md), [Rust prototype notes](history/rust-manager-prototype.md), and [Python prototype guide](history/python-prototype-guide.md) are preserved under `history/`. Their commands describe earlier revisions; use the current development guide for new work.

- [C#/Avalonia lifecycle port](avalonia-client.md): active client implementation and native acceptance.
