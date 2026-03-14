# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Gravity SDK is a modular blockchain framework built on Aptos. It provides a pipelined consensus engine using AptosBFT with 3-hop optimal latency, targeting ~160k TPS. The SDK uses GCEI (Gravity Consensus Execution Interface) to decouple consensus and execution layers.

## Build Commands

```bash
# Build gravity_node (requires tokio_unstable flag)
make BINARY=gravity_node MODE=release

# Quick release build (faster compilation)
make BINARY=gravity_node MODE=quick-release

# Build other binaries
make BINARY=bench
make BINARY=kvstore
make BINARY=gravity_cli   # via: cargo build --bin gravity_cli --profile quick-release

# Clean build artifacts
make clean
```

**Important**: The `gravity_node` binary requires `RUSTFLAGS="--cfg tokio_unstable"` which is handled automatically by the Makefile.

## Testing

```bash
# Run all unit tests
cargo test --workspace --exclude smoke-test

# Run a specific test
cargo test <test_name>

# Run tests for a specific crate
cargo test -p <crate_name>
```

### E2E Tests (Python)

```bash
cd gravity_e2e
pip install -r requirements.txt

# List available tests
python -m gravity_e2e.main --list-tests

# Run specific test suite
python -m gravity_e2e.main --test-suite basic
python -m gravity_e2e.main --test-suite contract
python -m gravity_e2e.main --test-suite erc20
python -m gravity_e2e.main --test-suite randomness
python -m gravity_e2e.main --test-suite cross_chain

# Run all tests
python -m gravity_e2e.main --test-suite all
```

## Code Quality

```bash
# Format check (requires nightly)
cargo +nightly fmt --all -- --check

# Lint with clippy
RUSTFLAGS="--cfg tokio_unstable" cargo clippy --all-targets --all-features -- -D warnings
```

## Architecture

### Workspace Structure

- **aptos-core/consensus**: AptosBFT consensus implementation with Quorum Store
- **crates/block-buffer-manager**: GCEI protocol - bridge between consensus and execution
- **crates/api**: REST/gRPC APIs (axum-based)
- **bin/gravity_node**: Main node binary (integrates with gravity-reth)
- **bin/gravity_cli**: CLI tool for node operations
- **bin/bench**: Benchmarking tool
- **dependencies/aptos-executor**: Custom executor implementation
- **gravity_e2e**: Python E2E testing framework

### Block Lifecycle (GCEI Protocol)

1. **Pre-Consensus**: Mempool collects transactions, Quorum Store batches them
2. **Consensus**: AptosBFT orders blocks, execution layer processes them
3. **Post-Consensus**: Results verified (2f+1 agreement), blocks committed

The BlockBufferManager manages block states: Ordered → Computed → Committed

### External Dependencies

- **gravity-reth**: `https://github.com/Galxe/gravity-reth` - EVM execution layer
- **gravity-aptos**: `https://github.com/Galxe/gravity-aptos` - Aptos fork (gaptos)

## CI/CD Notes

- Unit tests run on push to main, or when PR has `CICD:run-tests` label
- Format uses nightly rustfmt
- Clippy treats warnings as errors (`-D warnings`)
- Branch `branch-v*` requires matching `gravity-devnet-v*` branch in gravity-reth

## Rust Toolchain

- Version: 1.88.0 (specified in `rust-toolchain.toml`)
- Components: cargo, clippy, rustc, rust-docs, rust-std, rust-analyzer
- Format requires nightly: `cargo +nightly fmt`
