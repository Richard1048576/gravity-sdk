# pfn_chain VFN 压力测试 — 复现问题 1（PFN/VFN→Validator 性能衰减）

**日期**: 2026-05-09
**目标机器**: `zz@192.168.1.226` (Ubuntu 24.04, 28 vCPU, 31 GiB, 336 GB free)
**关注问题**: 已知 4 个压测问题中的 **问题 1** —— "Validator 12k 持续较久，VFN/PFN 只有 5-6k"
**作者**: Richard

## 0. 背景与定位

四个已知压测问题：
1. **(本次) PFN/VFN→Validator 性能衰减一半**（12k → 5-6k）
2. 压测中全网 stuck，VFN/PFN 不出块
3. 单节点压力下 mempool queue 满，出空块
4. Nonce mismatch 在拓扑变大时概率增加

本次只复现 **问题 1**。后三个问题如果在本次实验中自然 trigger，我们顺手记录、不主动追。问题 3 跟 pfn_chain 拓扑无关（应该用 `single_node`），不在本次范围。

### 0.1 已知 root cause 假设（同事 高鑫 提供）

实验设计前同事已经把代码路径走通，给出了具体定位：

| 维度 | bench → node1 RPC（约 12k TPS） | vfn1 → node1 broadcast（约 6k TPS） |
|---|---|---|
| 入口 | reth `eth_sendRawTransaction` | aptos mempool `MempoolDirectSend` |
| 走 BoundedExecutor 吗 | ❌ 不走（生产代码里 `MempoolClientRequest` channel 没人发） | ✅ 走（`coordinator.rs:122`） |
| 并发上限 | reth tokio runtime 工作线程数（机器 vCPU 级别） | **4** (`shared_mempool_max_concurrent_inbound_syncs`) |
| 单 batch 大小 | 1 tx | 300 tx (`shared_mempool_batch_size`) |

**root cause 假设**：bench 直连 validator RPC 时，tx 走 reth 的 RPC 接收路径，并发能拉满 vCPU；VFN 转发给 validator 时走 aptos mempool 的 `MempoolDirectSend` 路径，被 validator 端的 `BoundedExecutor`（容量 = `shared_mempool_max_concurrent_inbound_syncs = 4`）瓶颈住。

**代码验证**（`coordinator.rs` 第 92 行）：
```rust
let workers_available = smp.config.shared_mempool_max_concurrent_inbound_syncs;
let bounded_executor = BoundedExecutor::new(workers_available, executor.clone());
```
全局**单个** BoundedExecutor 跨所有 peer 共享 —— 不是 per-peer。所以即使有 N 个 VFN 同时打这个 validator，4 个 worker slot 仍然是总量上限。

**默认值**（`gravity-aptos@e9544c8/config/src/config/mempool_config.rs`）：
- `shared_mempool_max_concurrent_inbound_syncs = 4`
- `shared_mempool_batch_size = 300`
- `max_broadcasts_per_peer = 20`

**ConfigOptimizer 注入**（同文件第 191-220 行，未显式配置时）：
- Validator 节点：`max_broadcasts_per_peer` 20→**2**，`shared_mempool_batch_size` 300→**200**
- VFN 节点：`shared_mempool_max_concurrent_inbound_syncs` 4→**16**

⭐ 关键：optimizer **不**调 validator 的 `shared_mempool_max_concurrent_inbound_syncs`，所以 validator 收 mempool sync 的并发是死的 4。

**同事提议的修复值**（vfn1.yaml 实测达 8000+ tps）：
```yaml
mempool:
  shared_mempool_max_concurrent_inbound_syncs: 16
  shared_mempool_batch_size: 1000
  max_broadcasts_per_peer: 50
```

### 0.2 实验目标重定位

由于 root cause 已经定位到具体代码，本实验从**找 root cause** 转为 **验证假设 + 量化修复效果**：

1. **基线**（Phase 1）：用 pfn_chain 默认配置跑 4 目标对照，期望复现 node1 ≈ 8k / vfn1 ≈ 5-6k 的差距 → 验证假设
2. **修复后**（Phase 2，新增）：把同事提议的 mempool 三参数写进 validator + VFN yaml，重跑 4 目标对照，期望 vfn1/pfn1/pfn3 都接近 node1 → 量化修复效果
3. **对用户最后那个问题的代码侧回答**已经在 §0.1 给出（多 VFN 不能绕开）；如果时间允许，Phase 3 用 `four_validator` + 多 VFN 拓扑实测（暂未列入本 spec 范围，看 Phase 1/2 结果再说）

## 1. 拓扑与目标

直接复用 `gravity_e2e/cluster_test_cases/pfn_chain/cluster.toml`，全部跑在 `192.168.1.226` 单机 127.0.0.1 不同端口：

```
                    +-- pfn1 <--+
                    |           |
   node1 <-Vfn- vfn1+           +-- pfn3
                    |           |
                    +-- pfn2 <--+
```

**端口映射**（从 cluster.toml 确认）：

| 节点 | 角色 | RPC | reth metrics | aptos consensus metrics |
|---|---|---|---|---|
| node1 | validator (genesis) | 18545 | 9003 | 10002 |
| vfn1 | VFN (node1 shadow) | 18546 | 9004 | 10003 |
| pfn1 | PFN (sibling, dials vfn1) | 18547 | 9005 | 10004 |
| pfn2 | PFN (sibling, dials vfn1) | 18548 | 9006 | 10005 |
| pfn3 | PFN (leaf, dials pfn1+pfn2) | 18549 | 9007 | 10006 |

**主诊断目标**：vfn1（用户假设 VFN 是性能衰减的根源）。

**对照实验**：用同一份 `bench_config` 仅改 `rpc_url`，串行依次打 4 个目标各 5 分钟：

| 目标 | rpc_url | 期望（按已知现象） | 诊断价值 |
|---|---|---|---|
| node1 | `http://127.0.0.1:18545` | ~12k tps 持续稳定（ground truth） | 验证 validator 直连基线 |
| **vfn1** ⭐ | `http://127.0.0.1:18546` | 衰减到 5-6k | 主目标 |
| pfn1 | `http://127.0.0.1:18547` | ? | 二跳衰减程度 |
| pfn3 | `http://127.0.0.1:18549` | ? | 三跳衰减程度（pfn3→pfn1→vfn1→node1） |

**对照实验回答的问题**：衰减是只发生在 VFN，还是每过一跳都掉一截？
- 若 pfn1 / pfn3 跟 vfn1 一样衰减 → root cause 在 mempool 转发链路上、不是 VFN 特有
- 若只有 vfn1 衰减、PFN 跟 validator 直连差不多 → 集中在 VFN→Validator 这一跳
- 若每跳都掉一截（pfn3 < pfn1 < vfn1 < node1）→ broadcast 链路本身设计问题

## 2. 工具与配置

### 2.1 用现成的 `gravity_bench` 工具

`gravity_bench` 是独立 repo（`https://github.com/Galxe/gravity_bench.git`），226 上已有完整签出在 `/home/zz/Gravity/gravity_bench/`，binary 已编译（`target/release/gravity_bench`，4-22 build），`deploy.json`（ERC20 合约部署 manifest）也现成。

**完全不写新的发压代码**。

### 2.2 配置（基于同事 mainnet baseline 缩放到 226 单机）

mainnet baseline（同事提供）：`target_tps=8000`、`num_senders=500`、`num_accounts=100000`、`max_pool_size=40000`，单 RPC 节点。

226 单机限制：5 节点 + 1 bench 进程共享 28 vCPU / 31 GiB。500 senders × 数千 inflight × 5 reth RPC，CPU 会被 RPC 客户端先吃光，**不能照抄**。

**最终配置**（`gravity_bench/bench_config_pfn_chain.toml`）：

```toml
contract_config_path = "deploy.json"      # 复用 226 现成
target_tps = 8000                         # 对齐 mainnet baseline
nodes = [{ rpc_url = "<PHASE_TARGET>", chain_id = 1337 }]   # 各 phase 切换
num_tokens = 2
enable_swap_token = false
address_pool_type = "random"

[faucet]
private_key = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
faucet_level = 10
wait_duration_secs = 5
fauce_eth_balance = "1000000000000000000000000000"

[accounts]
num_accounts = 50000          # mainnet 100k 折半 — 减少 faucet 阶段耗时

[performance]
num_senders = <PHASE0_OPTIMAL>   # 由 Phase 0 决定，候选 100/200/400
max_pool_size = 20000            # mainnet 40k 折半
duration_secs = 300              # 5 min per phase
sampling = 50
```

**关键不同于 mainnet baseline 的两处**：
- `chain_id` 从 127001 改回 **1337**（pfn_chain genesis 默认）
- `num_accounts / num_senders / max_pool_size` 都缩了一半（候选 senders 待 Phase 0 决定）

## 3. 实验阶段

### Phase 0 — `num_senders` sanity check（~3 分钟）

3 次短跑找 226 单机最佳 sender 数：
- 目标固定为 vfn1（rpc=18546），**默认配置 cluster**
- `target_tps=8000`、`duration_secs=60`
- `num_senders ∈ {100, 200, 400}`，每档之间间隔 30s 让 mempool drain
- 评判标准（按优先级，从严到松）：
  1. 先剔除 `Success% < 95%` 的档（说明 client 自身打不动）
  2. 在剩下的档里挑 **稳态 TPS 最高** 的那个
  3. 若 3 档全 < 95%，记录失败，把 mainnet baseline 的 500 senders 也试一档（200 不够则 400 也不够，反推可能要更高）
- 输出：选出 Phase 1/2 共用的 `num_senders` 值

### Phase 1 — 默认配置基线，四目标对照（~22 分钟）

**目的**：验证 §0.1 的假设 —— 默认配置下应该复现 node1 ≈ 8k / vfn1 ≈ 5-6k / pfn 衰减。

按 `node1 → vfn1 → pfn1 → pfn3` 顺序串行跑 4 次：
- `target_tps=8000`、`num_senders=<phase0_optimal>`、`duration_secs=300`
- **cluster 用默认 mempool 配置**（即 cluster/templates/*.yaml.tpl 当前值，仅 `capacity_per_user: 20000`）
- 每次切换 `rpc_url`，cluster 不重启（保留 mempool/db 状态做累积观测）
- 每次之间 30s 冷却让前一目标的 mempool drain；冷却结束后用 `txpool_status` 检查 5 节点 mempool 都 < 1000，否则继续等（最多 60s）；超时则记录 warning 但继续
- 每个 phase 开始前先做 cluster 健康检查（`eth_blockNumber` 在 5 节点都返回非 null 且最大-最小差 < 50）；不健康则中止后续 phase，按 partial 报告
- bench log 各自存为 `bench_phase1_<target>_<ts>.log`

### Phase 2 — 应用 mempool 配置 patch，重跑四目标对照（~22 分钟）

**目的**：量化同事提议的 3 参数修复在 pfn_chain 拓扑下的实际效果。

1. **停 cluster**
2. **patch yaml**（cluster 启动后生成的 per-node config，**不**改 `cluster/templates/`，避免污染共享模板）。patch 工具用 Python `ruamel.yaml`（保留注释和顺序），写一个独立的小脚本 `scripts/pfn_pressure_patch_mempool.py`：
   - `node1/config/validator.yaml` 的 `mempool:` 块：
     ```yaml
     mempool:
       capacity_per_user: 20000
       shared_mempool_max_concurrent_inbound_syncs: 16    # 默认 4
       shared_mempool_batch_size: 1000                    # 默认 300，optimizer override 200
       max_broadcasts_per_peer: 50                        # 默认 20，optimizer override 2
     ```
   - `vfn1/config/validator_full_node.yaml` 同样改（VFN 也有 inbound — pfn1/pfn2 接进来 — 而且 outbound 用同一份 `shared_mempool_batch_size`/`max_broadcasts_per_peer`）
   - `pfn1`、`pfn2`、`pfn3` 的 `public_full_node.yaml` 同样改（虽然 PFN 不是主要瓶颈，但保持参数一致才能干净对比）
3. **重启 cluster**（保留 reth db 不动 → 链高从 Phase 1 末态续）
4. **重跑 Phase 1 的四目标对照**（同样的 target_tps / num_senders / duration / 顺序）
5. bench log 各自存为 `bench_phase2_<target>_<ts>.log`

### Phase 3 — 报告归档（~2 分钟）

- 每个 phase 用 `.agents/benchmark/gen_report.sh` 生成单独报告
- 手写一份汇总报告对比矩阵：

  | 目标 | Phase 1 默认 TPS | Phase 2 patch TPS | 提升倍数 |
  |---|---|---|---|
  | node1 | (基线) | (应该差不多) | ~1x |
  | vfn1 | 期望 5-6k | 期望 8k+ | 1.4-1.6x |
  | pfn1 | ? | ? | ? |
  | pfn3 | ? | ? | ? |

- 同时给出 Pool Pending / Pending Txns / `last_committed_round` 进度对比

**总耗时预估**：cluster 启动 + faucet（5 min）+ Phase 0（3 min）+ Phase 1（22 min）+ patch + 重启（3 min）+ Phase 2（22 min）+ Phase 3（2 min）≈ **57 分钟**。

## 4. 操作流程（226 上）

### 4.1 准备代码

```bash
ssh zz@192.168.1.226
cd /home/zz/Gravity/gravity-sdk
# 当前在 build/fix-cache (HEAD f6aa359) — PR #701 已合到 upstream/main，
# 这个分支可以丢弃但 build/fix-cache 引用保留以备回滚
git fetch upstream
git checkout -B test/pfn-pressure-20260509 upstream/main      # ec5f3853
```

### 4.2 编译

```bash
ulimit -n 65535        # 预检：避免 4-22 那次 "Too many open files" 重演
RUSTFLAGS="--cfg tokio_unstable" cargo build --bin gravity_node --profile quick-release
# 增量预计 3-5 分钟（保留了 build/fix-cache 的 cargo cache）
```

### 4.3 配 cluster_test_cases/pfn_chain 工作目录

```bash
cd gravity_e2e/cluster_test_cases/pfn_chain/
ls   # 应该有 cluster.toml、genesis.toml、test_pfn_chain.py
```

### 4.4 启 cluster（不跑 pytest，直接用 manager 起、保活）

写一个最小启动脚本 `/home/zz/Gravity/gravity-sdk/scripts/pfn_pressure_run.py`（约 80 行），职责：
1. 用 `gravity_e2e.cluster.manager.Cluster` 启动 5 节点
2. 等待全节点健康（`eth_blockNumber` 返回非 null）
3. 启动 sidecar collector（独立 thread/process）
4. 调用 gravity_bench 跑 Phase 0、Phase 1
5. 终止 collector + cluster + bench
6. 触发 `gen_report.sh`

### 4.5 sidecar collector

`/home/zz/Gravity/gravity-sdk/scripts/pfn_pressure_sidecar.py`（约 60 行），职责：
- 每 2 秒并发拉 5 节点的：
  - aptos consensus metrics (port 10002-10006)：抓 `aptos_consensus_(epoch|current_round|last_committed_round|proposals_count)`
  - `eth_blockNumber` (RPC 18545-18549)
  - reth `txpool_status` (RPC 18545-18549)
- 写 jsonl 到 `/tmp/gravity-cluster-pfn-chain/sidecar_metrics.jsonl`
- SIGTERM 即停

### 4.6 bench 调用

```bash
cd /home/zz/Gravity/gravity_bench
# Phase 0 sanity check (3 runs × 60s)
for senders in 100 200 400; do
  sed -i "s/num_senders = .*/num_senders = $senders/" bench_config_pfn_chain.toml
  ./target/release/gravity_bench --config bench_config_pfn_chain.toml \
    > log_phase0_senders${senders}_$(date +%s).log 2>&1
  sleep 30
done

# Phase 1 对照 (4 runs × 5min)
for target in node1:18545 vfn1:18546 pfn1:18547 pfn3:18549; do
  name=${target%:*}; port=${target#*:}
  sed -i "s|rpc_url = .*|rpc_url = \"http://127.0.0.1:$port\"|" bench_config_pfn_chain.toml
  ./target/release/gravity_bench --config bench_config_pfn_chain.toml \
    > log_phase1_${name}_$(date +%s).log 2>&1
  sleep 30
done
```

### 4.7 报告生成

```bash
for log in log_phase1_*.log; do
  ../gravity-sdk/.agents/benchmark/gen_report.sh \
    "$(cd ../gravity-sdk && git rev-parse --short HEAD)" \
    "$(git rev-parse --short HEAD)" \
    "$log"
done
```

汇总报告我手写到 `docs/superpowers/specs/2026-05-09-pfn-chain-vfn-pressure-test-results.md`。

## 5. 观测与判定

### 5.1 数据源

**(1) gravity_bench stdout**（主报告，**不写新代码**）—— 已有时序字段：
- `TPS`：实际达到的 tps
- `Avg Latency`：平均延迟
- `Success%`：成功率
- `Timed Out Txns`：超时数
- `Pending Txns`：bench 自身未确认 tx
- `Pool Pending` / `Pool Queued`：bench 通过 RPC `txpool_status` 拉的、目标节点的 reth txpool 状态

**(2) sidecar collector**（补 bench 看不见的部分）：
- 5 节点的 `aptos_consensus_*` 时序
- 5 节点 `eth_blockNumber` 时序
- 5 节点 `txpool_status`（bench 只采它打的那一个）

### 5.2 假设验证判定

实验从「找 root cause」转成「验证 §0.1 的假设」。两条判定独立：

#### 5.2.1 Phase 1（默认配置）—— 验证假设是否在 pfn_chain 单机环境下复现

输入：4 个目标都用相同的 `target_tps=8000` 和 sender 配置。

- **✅ 假设成立**：node1 稳态 TPS ≥ 8k * 0.9（≥7.2k），vfn1/pfn1/pfn3 稳态 TPS ≤ 6.5k；vfn1 / pfn 上 `Pool Pending` 长时间饱和、node1 上不饱和
- **❌ 假设不成立**：4 目标 TPS 差距 < 10%（本地单机条件下都打不出 4-worker 瓶颈，可能 Phase 0 的 senders 数不够把 mempool 真正灌满，或单机网络 RTT 太低使得 BoundedExecutor 来得及处理）→ 调高 senders 重试 / 改 mainnet baseline 数值
- **⚠ 部分成立**：差距 10%-30%

#### 5.2.2 Phase 2（patch 后）—— 验证修复效果

- **✅ 修复有效**：vfn1 稳态 TPS ≥ 7k（接近 node1 8k 水平），相比 Phase 1 的 vfn1 提升 ≥ 25%
- **❌ 修复无效**：vfn1 提升 < 10% → 假设不完全（可能还有别的瓶颈：网络 IO、reth tx pool admission、consensus 出块速率）
- **⚠ 部分有效**：提升 10%-25%

### 5.3 异常路径根因归类（任何阶段都可能 trigger）

如果实验中出现非预期现象（cluster 卡死、bench 大量 timeout 但 TPS 不升），用 sidecar 数据归类：

| 现象 | 推断 |
|---|---|
| bench 打 vfn1 时 node1 mempool 几乎空 | mempool 没顺利 forward 到 validator → 验证 §0.1 假设的直接证据 |
| node1 mempool 也满 | consensus 是瓶颈 → 跟 mempool forward 无关 |
| 5 节点 `last_committed_round` 进度差距大 | consensus 同步问题（可能是问题 2 的征兆） |
| pfn1 / pfn3 vs vfn1 差距单调累积 | broadcast 链路每跳都衰减 — 即使修复 validator 入口，PFN 路径仍可能受限 |

## 6. 风险与回退

### 6.1 已知风险

| 风险 | 概率 | 缓解 |
|---|---|---|
| `ulimit -n` 不够触发 "Too many open files" | 高（4-22 已经发生过） | 启动前 `ulimit -n 65535`；脚本预检 |
| 8k tps 把 reth 自身打死 / OOM | 中 | sidecar 监控 `reth metrics`；OOM 时停 + 报告 |
| bench 自身 CPU 不够 | 中 | Phase 0 的 senders 选择会暴露 |
| 编译失败（Rust toolchain 跟新 main 不兼容） | 低 | 先 `cargo check` 探路；不行回滚 build/fix-cache 编译路径 |
| 5 节点 + bench 实际跟 226 上现存的 mainnet_sim 端口冲突 | 低 | pfn_chain 用 18545-18549 / 9003-9007，mainnet_sim 是另一套 8552/8553 |
| Phase 1 后期 cluster 状态退化（mempool 累积、reth db 膨胀）影响公平性 | 中 | 每 phase 之间 30s 冷却；如果 4-th phase 跟 1st phase 量级差 > 20%，加一次中场 cluster 重启 |
| Phase 2 yaml patch 写错位置 / 字段名拼错 / yaml 解析失败 | 中 | patch 后启动前用 `gravity_node --check-config` 或 grep 确认字段确实出现在 yaml 中；如果 cluster 启动 5 节点中有任何一个 panic，立即 abort Phase 2 |
| Phase 2 重启后 cluster 起不来（identity 丢失、reth db 不兼容等） | 低 | 启动前 cluster_dir 整体打 tar 备份；起不来则 untar 回滚到 Phase 1 状态 |

### 6.2 回退路径

- 任意阶段 Ctrl-C → `pfn_pressure_run.py` 的 cleanup hook 杀 bench、sidecar、cluster 5 节点全部进程
- 不污染 226 现存 mainnet_sim：测试产物全部进 `/tmp/gravity-cluster-pfn-chain/`
- 编译有问题 → `git checkout build/fix-cache` 回到已知好的状态（之前 4-22 跑过的 binary）

### 6.3 不在本次范围

- 修代码（root cause 找到后，开 PR 是后续工作）
- 复现问题 2/3/4（自然 trigger 则记录，不主动追）
- Prometheus / Grafana 持久化监控（一次性实验，jsonl 够用）
- 多机分布式压测（mainnet baseline 是 bench 在独立机器上，本次单机够用，差距越大越能暴露问题 1）

## 7. 成功标准

本次实验**成功**的判定（不论假设是否成立）：
1. ✅ pfn_chain 5 节点 cluster 在 226 上跑通 ≥ 1 小时
2. ✅ gravity_bench 在 4 个目标 × 2 个 phase = 8 个 run 上各完成一次 5min 8k 压测
3. ✅ 产出 8 份 bench report + 1 份汇总对比报告 + sidecar jsonl
4. ✅ 给出 §5.2.1 假设验证判定 + §5.2.2 修复效果判定，附数据
5. ✅ 给用户的问题（多 VFN 是否能绕开问题）一个答复 —— **基于 §0.1 代码分析的答案是「不能」**，本实验数据可以进一步佐证（如果 Phase 2 patch 后 vfn1 单点能扛 8k+，说明 4 worker 确实是瓶颈）

不要求：fix 代码 PR / 复现问题 2-4 / 多机分布式压测 / four_validator+多 VFN 拓扑实测（如时间富裕再加 Phase 4）。
