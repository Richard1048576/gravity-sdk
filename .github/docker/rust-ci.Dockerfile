# Gravity SDK CI Runner - Rust Build Environment
# This image pre-installs system dependencies and downloads crates
# to speed up CI builds.
#
# Note: Pre-compilation is skipped due to disk space constraints.
# The rust-cache action will cache compiled artifacts between CI runs.

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
