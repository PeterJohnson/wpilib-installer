# Build only: none of the SDK or compiler ships in the application.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl clang zlib1g-dev libssl3 python3 && rm -rf /var/lib/apt/lists/*
RUN mkdir /dotnet && curl -fsSL https://builds.dotnet.microsoft.com/dotnet/Sdk/10.0.404/dotnet-sdk-10.0.404-linux-x64.tar.gz | tar -xz -C /dotnet
ENV PATH=/dotnet:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin DOTNET_ROOT=/dotnet DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
WORKDIR /work
