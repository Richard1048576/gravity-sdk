# Gravity SDK - Agent Handoff 文档

> 最后更新: 2026-02-02
> 分支: main (Richard1048576/gravity-sdk fork)
> 上游: Galxe/gravity-sdk

## 1. 项目概述

Gravity SDK 是基于 Aptos 构建的模块化区块链框架。使用 AptosBFT 共识引擎，通过 GCEI (Gravity Consensus Execution Interface) 解耦共识与执行层，目标 ~160k TPS。

### 仓库关系

- **本仓库**: `Richard1048576/gravity-sdk` (fork)
- **上游**: `Galxe/gravity-sdk`
- **EVM 执行层**: `Galxe/gravity-reth`
- **Aptos fork**: `Galxe/gravity-aptos` (gaptos)

### Git Remotes

```
origin    https://github.com/Richard1048576/gravity-sdk.git
upstream  https://github.com/Galxe/gravity-sdk.git
```

## 2. 最近完成的工作 (CI/CD 优化)

### 2.1 已解决的核心问题

**磁盘空间不足**: GitHub Actions runner 只有 ~14GB，Rust 编译产物过大。

解决方案:
1. **自定义 Docker 镜像** (`ghcr.io/richard1048576/gravity-sdk/rust-ci:latest`)
   - 基于 `rust:1.88.0-bookworm`，预装 clang/llvm/libudev/libssl/pkg-config
   - 预编译依赖缓存在 `/opt/target-cache`，构建时 `cp -rn` 到 workspace
   - 镜像构建 workflow: `.github/workflows/build-ci-image.yml`

2. **测试并行化** - 将单个 test job 拆分为 4 个 matrix group:
   - `core`: gravity-primitives, gravity-storage, api, api-types
   - `consensus`: block-buffer-manager, txn_metrics, aptos-consensus 系列
   - `gaptos`: gaptos, aptos-executor
   - `binaries`: gravity_node, gravity_cli

3. **磁盘清理**: 每个 job 开始前清理 dotnet/android/ghc 等大型包

### 2.2 当前 CI Workflows

| 文件 | 用途 | 触发条件 |
|------|------|----------|
| `rust-ci.yml` | 主 CI (fmt + clippy + build + test) | push main/branch-v*, PR, 手动 |
| `nightly-tests.yml` | Debug 模式全量测试 | 仅手动触发 (schedule 已移除) |
| `build-ci-image.yml` | 构建 Docker 镜像 | 手动 / Dockerfile 变更 |
| `e2e-docker.yml` | E2E 测试 | - |
| `benchmark-docker.yml` | 性能基准测试 | - |

### 2.3 CI 关键配置

- 测试需要 `CICD:run-tests` label (PR) 或 push to main 才会跑
- Clippy 使用 `--profile ci`，warnings 视为 errors
- 格式化使用 nightly rustfmt
- `branch-v*` 分支要求 gravity-reth 依赖使用对应的 `gravity-devnet-v*` 分支

### 2.4 最近的 CI Commits 记录

```
5100f51 feat(ci): parallelize test jobs into 4 matrix groups
05227f1 fix: keep cargo git checkouts in Docker image
5f1af6a fix: remove redundant rust-cache and increase disk cleanup
eae0036 fix: force fresh Docker build without cache
139095e feat(ci): pre-compile dependencies in Docker image
c4ecebe fix(ci): optimize disk space and use Docker with pre-fetched cargo deps
b9d67b3 fix(ci): use richard1048576 image for testing
e33ae3b feat(ci): change Docker image build to daily schedule
32c60ac refactor(ci): clean up and consolidate CI/CD configuration
```

## 3. 架构要点

### 3.1 Workspace 结构

- `aptos-core/consensus/` - AptosBFT 共识 (从 gaptos 复制+修改)
- `crates/block-buffer-manager/` - GCEI 协议桥接
- `crates/api/` - REST/gRPC API (axum)
- `bin/gravity_node/` - 主节点 (集成 gravity-reth)
- `bin/gravity_cli/` - CLI 工具
- `bin/bench/` - 基准测试工具
- `dependencies/aptos-executor/` - 自定义执行器

### 3.2 Gravity 特有代码

仅 2 个文件是 Gravity 专属 (非 gaptos 复制):
- `aptos-core/consensus/src/gravity_state_computer.rs` (194 行) - GCEI 集成核心
- `aptos-core/consensus/src/qc_aggregator.rs` (~50 行) - QC 聚合

### 3.3 GCEI Block 生命周期

1. Pre-Consensus: Mempool 收集交易 → Quorum Store 批处理
2. Consensus: AptosBFT 排序区块 → 执行层处理
3. Post-Consensus: 结果验证 (2f+1 agreement) → 提交

BlockBufferManager 管理状态: Ordered → Computed → Committed

## 4. 构建与测试

```bash
# 构建
make BINARY=gravity_node MODE=release
make BINARY=gravity_node MODE=quick-release  # 快速编译

# 单元测试
cargo test --workspace --exclude smoke-test

# E2E 测试
cd gravity_e2e && python -m gravity_e2e.main --test-suite all

# 代码质量
cargo +nightly fmt --all -- --check
RUSTFLAGS="--cfg tokio_unstable" cargo clippy --all-targets --all-features -- -D warnings
```

Rust 工具链: 1.88.0 (`rust-toolchain.toml`)

## 5. 待办事项

### 5.1 测试迁移 (详见 `todo/test-migration-analysis.md`)

从 gaptos 迁移 ~157 个测试，按优先级:
- P0 EASY: Block Types (7), ConsensusDB (6) - 可直接迁移
- P1 MEDIUM: Block Storage (10), Quorum Store (32) - 需 API 适配
- P1 HARD: Safety Rules (19) - 需重构
- P2 HARD: DAG (20), Round Manager (24), Pipeline (5)

### 5.2 CI 后续优化

- [ ] 添加 sccache 加速编译
- [ ] 修复/禁用不稳定测试 (如 `crates/api` 的 https 测试)
- [ ] 评估更大 runner

### 5.3 架构同步 (详见 `todo/architecture-sync-plan.md`)

- [ ] 评估同步 gaptos 的 PayloadTxnsSize 安全结构体
- [ ] 重构 consensus_observer 为模块化
- [ ] 评估将 GCEI 贡献回 gravity-aptos
- [ ] 建立定期同步机制

## 6. 已知问题

1. **大部分 crate 无测试**: 只有 `aptos-consensus` 有 213 个测试，其他 crate 测试为 0
2. **API https 测试不稳定**: `crates/api/src/https/mod.rs` 的集成测试依赖 HTTPS 服务器启动
3. **Rust 编译诊断**: `lib.rs:47` 有 `disable_lifo_slot` 方法不存在的错误 (tokio API 变更)
4. **Safety Rules 测试**: 需要 `--skip tests::thread --test-threads=1` 来避免并发问题
5. **DAG E2E 测试**: `integration_tests::test_dag_e2e` 被 skip，不稳定

## 7. 相关文档索引

| 文件 | 内容 |
|------|------|
| `CLAUDE.md` | 项目构建指南和 agent 指令 |
| `todo/ci-optimization.md` | CI 优化详细记录 |
| `todo/architecture-sync-plan.md` | 架构同步计划 |
| `todo/test-migration-analysis.md` | 测试迁移分析 (157 tests) |
| `.github/docker/rust-ci.Dockerfile` | CI Docker 镜像定义 |
