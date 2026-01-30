# Gravity SDK CI Runner
#
# This image provides a pre-configured Rust build environment for CI.
# It includes:
# - Rust 1.88.0 with nightly rustfmt and clippy
# - System build dependencies (clang, llvm, etc.)
# - Pre-fetched cargo registry and git dependencies
# - Environment variables for tokio_unstable
#
# Build:
#   docker buildx build --platform linux/amd64 -f .github/docker/rust-ci.Dockerfile -t rust-ci .
#
# Push:
#   docker push ghcr.io/galxe/gravity-sdk/rust-ci:latest

FROM rust:1.88.0-bookworm

LABEL maintainer="Gravity Team"
LABEL description="CI environment for Gravity SDK"

# Install system build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    clang \
    llvm \
    libudev-dev \
    libssl-dev \
    pkg-config \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV CARGO_TERM_COLOR=always
ENV CARGO_INCREMENTAL=0
ENV RUSTFLAGS="--cfg tokio_unstable"

# Install rustfmt nightly and clippy for CI checks
RUN rustup toolchain install nightly --component rustfmt
RUN rustup component add clippy

# Clone repo and fetch all dependencies
WORKDIR /tmp
RUN git clone --depth 1 https://github.com/Galxe/gravity-sdk.git && \
    cd gravity-sdk && \
    cargo fetch && \
    cd / && \
    rm -rf /tmp/gravity-sdk

# Show cache size
RUN du -sh /usr/local/cargo/registry /usr/local/cargo/git 2>/dev/null || true

# Set working directory for CI
WORKDIR /workspace

# Default command
CMD ["/bin/bash"]
