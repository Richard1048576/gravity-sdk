# Gravity SDK CI Runner
#
# This image provides a pre-configured Rust build environment for CI.
# It includes:
# - Rust 1.88.0 with nightly rustfmt and clippy
# - System build dependencies (clang, llvm, etc.)
# - Pre-compiled dependencies (in /opt/target-cache)
# - Environment variables for tokio_unstable
#
# Build:
#   docker buildx build --platform linux/amd64 -f .github/docker/rust-ci.Dockerfile -t rust-ci .
#
# Push:
#   docker push ghcr.io/richard1048576/gravity-sdk/rust-ci:latest

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

# Clone repo and pre-compile dependencies
WORKDIR /workspace
RUN git clone --depth 1 https://github.com/Richard1048576/gravity-sdk.git . && \
    # Pre-compile all dependencies with CI profile (limit jobs to reduce memory usage)
    cargo build --profile ci --tests --workspace --exclude smoke-test -j 2 && \
    # Move compiled target to cache location
    mv target /opt/target-cache && \
    # Clean up source code (will be provided by CI)
    rm -rf /workspace/* /workspace/.* 2>/dev/null || true && \
    # Delete unpacked source files but keep compressed crates and index
    rm -rf /usr/local/cargo/registry/src && \
    # Note: Keep /usr/local/cargo/git/checkouts - cargo needs these at runtime
    # Show sizes
    echo "=== Cache sizes ===" && \
    du -sh /opt/target-cache && \
    du -sh /usr/local/cargo/registry 2>/dev/null || true && \
    du -sh /usr/local/cargo/git 2>/dev/null || true

# Set working directory for CI
WORKDIR /workspace

# Default command
CMD ["/bin/bash"]
