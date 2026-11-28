# Rust reference client

This is the retained Rust implementation used before selecting C#/Avalonia. The active client is [`client-dotnet/`](../client-dotnet/), built by `tools/build_client.py`. Build this reference explicitly with `tools/build_rust_client.py`; its Cargo/IFW requirements and embedded-asset contract remain unchanged.

Its package, installed-state and trust/cache formats serve as compatibility references. Acceptance evidence produced before the C# migration applies to this implementation, not automatically to the new client. See the [C# port report](../docs/avalonia-client.md).
