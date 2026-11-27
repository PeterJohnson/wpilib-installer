# Development guide

## Prerequisites

Python 3.11+ is for build, publishing, and test tooling. Rust 1.100.0 is the pinned client toolchain. Qt Installer Framework 4.11.0 is pinned in `tools/ifw-lock.json`; the bootstrap verifies its archive hash. Download extraction requires `7z` or `7zz`.

On Ubuntu build hosts, `.github/scripts/install-linux-dependencies.sh` installs Qt tool dependencies and the extractor. Linux client compilation runs in Docker on an Ubuntu 22.04 baseline and includes its Rust toolchain. Native GUI tests additionally need a display or Xvfb. Clean Desktop acceptance uses host-only QEMU/KVM and xorriso; no packages are installed in the Desktop guest. These are development requirements, not end-user prerequisites.

Windows native builds require MSVC build tools and NASM. macOS builds use Xcode command-line tools and `sevenzip`; deployment target remains macOS 13, with macOS 15 CI on Intel and ARM. Linux/Windows ARM client releases are not enabled.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[ci]'
python tools/bootstrap_ifw.py
```

On Windows use `python` and activate `.venv\Scripts\Activate.ps1`. Never commit generated private keys. Use a new build directory for each fixture preparation run.

## Client build and local checks

```sh
python tools/prepare_client_fixture.py --ifw .tools/ifw --work build/client-fixture
python tools/build_client.py --source build/client-fixture/demo/registry-source.json --ifw .tools/ifw --assets build/client/assets --output build/client/download
cargo fmt --manifest-path client/Cargo.toml --check
```

The Linux builder runs release-mode unit tests inside the baseline container. On Windows/macOS, set `WPILIB_ASSET_DIR` to the absolute `build/client/assets` path before `cargo test --locked --release --manifest-path client/Cargo.toml`; set `MACOSX_DEPLOYMENT_TARGET=13.0` on macOS.

The executable is `build/client/download/wpilib-manager` on Linux, `WPILibManager.exe` on Windows, or `WPILib Manager.app/Contents/MacOS/wpilib-manager` on macOS. Build output includes a ZIP preserving Unix modes and a dependency audit. The bootstrap supplied to the build fixes public trust, target, season, and channel. Fixture bootstraps authorize disposable test repositories only.

For the offline client lifecycle on Linux:

```sh
xvfb-run -a python tools/check_client_offline.py --app build/client/download/wpilib-manager --baseline build/client-fixture/baseline/*.zip --candidate build/client-fixture/candidate/*.zip --work build/client-fixture/native --report build/client-fixture/report.json
```

For authenticated online tests, prepare with `tools/prepare_client_online.py`, build against its `online-source.json`, and invoke `tools/check_client_online.py`. That fixture deliberately uses a loopback HTTP exception and an expired/rotated root history; never use its bootstrap for deployment.

## Publishing and tooling tests

```sh
python -m unittest discover -v
IFW_PATH=.tools/ifw python -m unittest discover -v
python -m installer_tooling --help
wpilib-installer-tools --help
```

PowerShell uses `$env:IFW_PATH='.tools/ifw'`. Tests requiring Qt skip when IFW is absent; a passing run with skips is not native acceptance. Publication CI installs the tooling as a wheel before testing, so package resources and the renamed namespace are exercised.

`installer_tooling.qt_engine` builds the private worker and reads Qt inventory. `installer_tooling.reference_client` is the Python comparison adapter used by publication gates and regression tests. It is not the installed client. The on-disk installation/cache schemas, package IDs, repository formats, and archival policies are shared contracts; reorganizing code is not permission to change them.

## CI ownership

| Workflow | Role |
| --- | --- |
| `client.yml` | Four native client builds, authenticated update/cache checks, minimal Linux headless checks, and dependent clean Desktop fixture checks. |
| `tooling.yml` | Four-platform publishing/tooling regressions and native offline publication gates. Emits evidence, not an end-user Qt-only installer. |
| `desktop-real.yml` | Pinned real dashboard bundles plus fresh Ubuntu 24.04/26.04 Desktop offline installation, upgrades, and cache maintenance. |
| `desktop-acceptance.yml` | Reusable/manual Desktop VM harness; can retest an existing `ubuntu-desktop-test-input` artifact. |
| `dashboard-releases.yml` | Native exact-package publication checks for real dashboard recipes on all four targets. |
| `client-offline.yml`, `client-online.yml` | Explicit extended native suites retained for fixture and real-package regression work. |
| Publisher/registry renewal and offline-bundle workflows | Deployment automation, requiring reviewed configuration and appropriate signing/storage secrets. |

Example dispatches:

```sh
gh workflow run client.yml -f platform=all
gh workflow run desktop-real.yml
gh workflow run desktop-acceptance.yml -f application_run=RUN_ID -f release=all -f require_real=true
```

Changes to workflow names require updating any externally configured branch-protection checks. CI names alone do not configure branch protection or authorize a release. Follow every dispatched required run to completion and save evidence for release acceptance.

## Migration from the prototype layout

`manager/` is now `client/`; the Python package is under `tooling/installer_tooling/`. Build/test scripts use `client` or their platform responsibility instead of `rust-manager` names. Historical comparisons and prototype commands remain under `docs/history/`.

For an existing development virtualenv, uninstall `wpilib-installer-prototype` before reinstalling this project's tooling. This removes its old Python `wpilib-manager` entry point, which would otherwise conflict with the native application's name:

```sh
python -m pip uninstall wpilib-installer-prototype
python -m pip install -e '.[ci]'
```

The new Python entry point is `wpilib-installer-tools`. Existing native installations and bundles do not need conversion: `.wpilib-manager.json`, verified cache locations, Qt inventory, and trust formats are unchanged. Fresh development installs default to `~/wpilib-packages`; the installed manager still discovers its existing root, and `--root` selects an older installation explicitly. No existing installation is moved automatically. Final season-based paths and OS integration remain release work.
