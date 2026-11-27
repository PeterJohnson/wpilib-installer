# Test infrastructure only. The manager is tested in separate unmodified OS images.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends xvfb weston \
    && rm -rf /var/lib/apt/lists/*
