"""Build the portable Linux manager and its runtime on Ubuntu 22.04, using Docker."""
import os
from pathlib import Path
import subprocess


def build_linux(assets):
    repo = Path(__file__).resolve().parents[1]
    assets = Path(assets).resolve()
    cache = repo / 'build/linux-cargo'
    cache.mkdir(parents=True, exist_ok=True)
    image = 'wpilib-installer-linux-runtime:22.04-rust-1.100.0'
    subprocess.run(['docker', 'build', '-f', '.github/containers/linux-runtime.Dockerfile', '-t', image, '.'],
                   cwd=repo, check=True)
    subprocess.run(['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}',
                    '-v', f'{repo}:/work', '-v', f'{assets}:/assets', '-v', f'{cache}:/cargo',
                    '-e', 'CARGO_HOME=/cargo', '-e', 'WPILIB_ASSET_DIR=/assets',
                    '-e', 'CARGO_TARGET_DIR=/work/manager/target/linux-contained',
                    image, 'bash', '-euc',
                    'python3 tools/package_rust_runtime.py --engine /assets/engine --output /assets/runtime.zip\n'
                    'cargo build --locked --release --manifest-path manager/Cargo.toml\n'
                    'cargo test --locked --release --manifest-path manager/Cargo.toml'], check=True)
    return repo / 'manager/target/linux-contained/release/wpilib-manager'
