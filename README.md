# WPILib Installer

The active implementation is a **single NativeAOT C#/Avalonia application with a private Qt installation engine**. It provides a GUI and JSON CLI, authenticated online package updates, offline installation, and maintenance from retained verified caches. Publishers release independently; an automated assembler produces the latest compatible package set for one platform and a browser download page.

**This is the production implementation under development, not a production-ready distribution.** The architecture has been selected and its main paths have been exercised. SDK packaging, deployment, signing, recovery, platform integration, and release acceptance still have work to do. See the [production-readiness plan](docs/production-readiness.md).

End users do not install Python, Qt, .NET, or FUSE. Linux uses host graphics and keeps the Qt worker's dependencies private. NativeAOT, Avalonia native libraries and the private worker are bundled; no .NET runtime is installed. See the [runtime policy and acceptance scope](docs/self-contained-runtime.md).

## Repository layout

| Directory | Responsibility |
| --- | --- |
| `client/` | Installed C#/Avalonia application: GUI, CLI, TUF verification, caches, package planning, and private Qt worker orchestration. |
| `tooling/installer_tooling/` | Python publishing, registry renewal, offline assembly, package recipes, and publication acceptance tooling. Never shipped as the installed manager. |
| `tools/` | Build, bootstrap, fixture preparation, and acceptance entry points. |
| `packaging/` | Pinned real upstream release recipes and upgrade pairs. Core SDK and RobotPy recipes remain to be implemented. |
| `deployment/` | Signing roles, registry configuration, Artifactory staging/promotion, R2/S3, and offline download hosting. |
| `tests/` | Publishing/tooling regression tests. Native client acceptance lives under `tools/`. |
| `docs/` | Current architecture, client/runtime contracts, package catalog and release-readiness plan. |

## Develop and build

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for native prerequisites, commands, testing, and migration from the earlier layout. A disposable Linux fixture build is:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[ci]'
python tools/bootstrap_ifw.py
python tools/prepare_client_fixture.py --ifw .tools/ifw --work build/client-fixture
python tools/build_client.py --source build/client-fixture/demo/registry-source.json --ifw .tools/ifw --assets build/client/assets --output build/client/download
build/client/download/wpilib-manager --bundle build/client-fixture/baseline/*.zip --root build/demo-installation
```

Linux builds use Docker and the Ubuntu 22.04 ABI baseline. Windows/macOS build on native runners. These commands generate synthetic packages and throwaway trust keys. The resulting application and CI test artifacts are **test distributions**, not production installers. No signing secrets are embedded in the application.

Real Elastic and AdvantageScope packaging uses [reviewed, hash-pinned releases](packaging/README.md). The [clean Desktop real-package gate](.github/workflows/desktop-real.yml) installs and upgrades both applications on fresh Ubuntu Desktop systems, then exercises cached maintenance after deleting the original installer and media. It does not launch the installed dashboard applications.

## Publishing and trust

Use the `wpilib-installer-tools` development CLI or `python -m installer_tooling`; the installed application's CLI is `wpilib-manager`. The Python acceptance client supports publication gates and regression tests.

- [Client operations and acceptance](docs/client.md)
- [Architecture and repository contracts](docs/architecture.md)
- [TUF repositories, signing, renewal, and archival trust](deployment/tuf.md)
- [Publisher CI and signing secrets](deployment/ci-signing.md)
- [Artifactory staging and promotion](deployment/artifactory.md)
- [Offline bundle and browser-page publication](deployment/offline-bundles.md)
- [Deployment registry configuration](deployment/registries/README.md)

Online trust and indefinite offline archival trust remain separate. A bundle preserves its authenticated registry revision; adding publishers does not require rebuilding the installed client. Failed assembly or publication preserves the last successful offline download.

Production registry roots, archival keys, endpoints, and release approval are deployment inputs. The repository does not enable a production client release merely because a test build succeeds. See the [release gates](docs/production-readiness.md#release-gates).

Project code uses [BSD-3-Clause](LICENSE.md). Qt and packaged upstream dependencies retain their licenses and redistribution obligations.
