# Build only. No compiler, Python, package manager, or FUSE ships to the client.
FROM rust:1.100.0-slim-bookworm AS rust
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl pkg-config python3 binutils \
    libbz2-1.0 liblzma5 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 \
    libxcb-sync1 libx11-xcb1 libfontconfig1 libdbus-1-3 \
    libxcursor1 libxi6 libxrandr2 libxinerama1 libxrender1 libxext6 \
    libgl1 libegl1 libglx-mesa0 libegl-mesa0 libgl1-mesa-dri \
    libwayland-client0 libwayland-cursor0 libwayland-egl1 \
    fonts-dejavu-core xkb-data libx11-data \
    && rm -rf /var/lib/apt/lists/*
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=rust /usr/local/cargo/bin /usr/local/cargo/bin
ENV PATH=/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    RUSTUP_HOME=/usr/local/rustup
WORKDIR /work
