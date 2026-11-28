# Build only. No compiler, Python, package manager, or FUSE ships to the client.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates python3 binutils libgcc-s1 libstdc++6 \
    libbz2-1.0 liblzma5 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 \
    libxcb-sync1 libxcb-randr0 libx11-xcb1 libfontconfig1 libdbus-1-3 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
