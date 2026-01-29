# Gravity SDK CI Runner
#
# This image provides a pre-configured Rust build environment for CI.
# It includes:
# - Rust 1.88.0 with nightly rustfmt
# - System build dependencies (clang, llvm, etc.)
# - Environment variables for tokio_unstable
#
# For local builds with pre-compiled dependencies, use:
#   docker build -f .github/docker/rust-ci.Dockerfile -t rust-ci .
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

# Install rustfmt nightly for formatting checks
RUN rustup toolchain install nightly --component rustfmt

# Set working directory for CI
WORKDIR /github/workspace

# Default command
CMD ["/bin/bash"]
