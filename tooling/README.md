# Publishing and acceptance tooling

`installer_tooling` is the Python package for repository publication, TUF metadata renewal, offline bundle assembly/hosting, package recipes, and native publication gates. Install it from the repository root with `python -m pip install -e '.[ci]'`.

The CLI is `wpilib-installer-tools` or `python -m installer_tooling`. The end-user GUI/CLI is the native application in [`client-dotnet/`](../client-dotnet/); Python is never a prerequisite for that application.

`qt_engine.py` owns worker construction and inventory inspection. `reference_client.py` retains the Python installation adapter used as a regression/publication test oracle. `demo.py` and `assets/demo.json` are synthetic fixture data; their package IDs, marker paths, and `prototype-fixtures` wire value remain intentionally unchanged.

See [deployment](../deployment/), [package recipes](../packaging/README.md), and the [development guide](../CONTRIBUTING.md). Generated private keys and payloads belong in ignored build directories.
