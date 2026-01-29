# Gravity SDK CI Runner - Two-stage build for pre-compiled dependencies
#
# Strategy:
# 1. Stage 1 (builder): Compile all dependencies in release mode
# 2. Clean up intermediate artifacts (incremental cache, fingerprints, etc.)
# 3. Stage 2 (final): Copy only registry + compiled .rlib files
#
# This reduces the final image size while keeping compiled dependencies.

# =============================================================================
# Stage 1: Builder - Compile dependencies
# =============================================================================
FROM rust:1.88.0-bookworm AS builder

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

WORKDIR /prebuild

# Copy workspace Cargo files
COPY Cargo.toml Cargo.lock ./

# Copy all crate Cargo.toml files (only files that actually exist in repo)
COPY aptos-core/consensus/Cargo.toml aptos-core/consensus/
COPY aptos-core/consensus/consensus-types/Cargo.toml aptos-core/consensus/consensus-types/
COPY aptos-core/consensus/safety-rules/Cargo.toml aptos-core/consensus/safety-rules/
COPY aptos-core/mempool/Cargo.toml aptos-core/mempool/
COPY bin/bench/Cargo.toml bin/bench/
COPY bin/gravity_cli/Cargo.toml bin/gravity_cli/
COPY bin/gravity_node/Cargo.toml bin/gravity_node/
COPY crates/api/Cargo.toml crates/api/
COPY crates/block-buffer-manager/Cargo.toml crates/block-buffer-manager/
COPY crates/build-info/Cargo.toml crates/build-info/
COPY crates/gravity-sdk/Cargo.toml crates/gravity-sdk/
COPY crates/txn_metrics/Cargo.toml crates/txn_metrics/
COPY dependencies/aptos-executor/Cargo.toml dependencies/aptos-executor/
COPY dependencies/aptos-executor-types/Cargo.toml dependencies/aptos-executor-types/

# Create dummy source files for cargo to compile dependencies
RUN mkdir -p aptos-core/consensus/src && echo "pub fn _dummy() {}" > aptos-core/consensus/src/lib.rs && \
    mkdir -p aptos-core/consensus/consensus-types/src && echo "pub fn _dummy() {}" > aptos-core/consensus/consensus-types/src/lib.rs && \
    mkdir -p aptos-core/consensus/safety-rules/src && echo "pub fn _dummy() {}" > aptos-core/consensus/safety-rules/src/lib.rs && \
    mkdir -p aptos-core/mempool/src && echo "pub fn _dummy() {}" > aptos-core/mempool/src/lib.rs && \
    mkdir -p bin/bench/src && echo "fn main() {}" > bin/bench/src/main.rs && \
    mkdir -p bin/gravity_cli/src && echo "fn main() {}" > bin/gravity_cli/src/main.rs && \
    mkdir -p bin/gravity_node/src && echo "fn main() {}" > bin/gravity_node/src/main.rs && \
    mkdir -p crates/api/src && echo "pub fn _dummy() {}" > crates/api/src/lib.rs && \
    mkdir -p crates/block-buffer-manager/src && echo "pub fn _dummy() {}" > crates/block-buffer-manager/src/lib.rs && \
    mkdir -p crates/build-info/src && echo "pub fn _dummy() {}" > crates/build-info/src/lib.rs && \
    mkdir -p crates/gravity-sdk/src && echo "pub fn _dummy() {}" > crates/gravity-sdk/src/lib.rs && \
    mkdir -p crates/txn_metrics/src && echo "pub fn _dummy() {}" > crates/txn_metrics/src/lib.rs && \
    mkdir -p dependencies/aptos-executor/src && echo "pub fn _dummy() {}" > dependencies/aptos-executor/src/lib.rs && \
    mkdir -p dependencies/aptos-executor-types/src && echo "pub fn _dummy() {}" > dependencies/aptos-executor-types/src/lib.rs

# Fetch all dependencies first
RUN cargo fetch

# Build dependencies in release mode (will fail on dummy sources, but deps get compiled)
RUN cargo build --release 2>/dev/null || true

# Show size before cleanup
RUN echo "=== Before cleanup ===" && du -sh /usr/local/cargo/registry /usr/local/cargo/git target/release 2>/dev/null || true

# Aggressive cleanup - remove everything except compiled .rlib files
RUN rm -rf target/release/incremental && \
    rm -rf target/release/.fingerprint && \
    rm -rf target/release/build && \
    rm -rf target/release/examples && \
    rm -rf target/release/deps/*.d && \
    rm -rf target/release/deps/*.rmeta && \
    find target/release/deps -type f ! -name "*.rlib" -delete 2>/dev/null || true

# Show size after cleanup
RUN echo "=== After cleanup ===" && du -sh /usr/local/cargo/registry /usr/local/cargo/git target/release/deps 2>/dev/null || true

# =============================================================================
# Stage 2: Final image - slim with pre-compiled deps
# =============================================================================
FROM rust:1.88.0-bookworm

LABEL maintainer="Gravity Team"
LABEL description="CI environment for Gravity SDK with pre-compiled release dependencies"

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

# Copy cargo registry (downloaded crate sources)
COPY --from=builder /usr/local/cargo/registry /usr/local/cargo/registry
COPY --from=builder /usr/local/cargo/git /usr/local/cargo/git

# Copy pre-compiled release dependencies
COPY --from=builder /prebuild/target/release/deps /prebuilt-deps/release/

# Set working directory for CI
WORKDIR /github/workspace

# Default command
CMD ["/bin/bash"]
