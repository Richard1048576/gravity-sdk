# Gravity VFN 同步逻辑详解

本文档详细介绍 Gravity SDK 中 VFN (Validator Full Node) 与 Validator 节点建立连接和区块同步的完整流程。

## 1. 架构概览

### 1.1 VFN 在 Gravity 中的定位

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Gravity VFN 网络架构                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌─────────────┐      Validator Network      ┌─────────────┐              │
│   │  Validator  │◄────────────────────────────►│  Validator  │              │
│   │   (Epoch)   │      (双向认证, 共识)         │   (Epoch)   │              │
│   └──────┬──────┘                              └──────┬──────┘              │
│          │                                           │                      │
│          │ VFN Network (onchain discovery)           │                      │
│          │                                           │                      │
│          ▼                                           ▼                      │
│   ┌─────────────┐                              ┌─────────────┐              │
│   │     VFN     │◄────────────────────────────►│     VFN     │              │
│   │ (非当前Epoch │      (区块同步)              │ (非当前Epoch │              │
│   │   验证者)    │                              │   验证者)    │              │
│   └─────────────┘                              └─────────────┘              │
│                                                                             │
│   用途: 非当前 epoch 的验证者通过 VFN 网络同步区块                            │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 VFN vs Validator 角色区分

**代码位置：** `crates/api/src/consensus_api.rs:161-169`

```rust
let mut network_builder = NetworkBuilder::create(
    chain_id,
    if network_id.is_vfn_network() {
        // FIXME(nekomoto): This is a temporary solution to support block sync for
        // validator node which is not the current epoch validator.
        RoleType::FullNode  // ← VFN 使用 FullNode 角色
    } else {
        node_config.base.role  // ← Validator 网络使用配置角色
    },
    &network_config,
    // ...
);
```

**设计意图：** 当验证者不在当前 epoch 的验证者集合中时，需要通过 VFN 网络同步区块，此时角色临时切换为 FullNode。

## 2. VFN 配置结构

### 2.1 VFN 节点配置

**文件位置：** `deploy_utils/vfn/config/validator_full_node.yaml`

```yaml
base:
  role: "full_node"
  data_dir: "/tmp/vfn/data"
  waypoint:
    from_file: "/tmp/vfn/config/waypoint.txt"

full_node_networks:
  - network_id:
      private: "vfn"
    listen_address: "/ip4/127.0.0.1/tcp/2044"
    identity:
      type: "from_file"
      path: /tmp/vfn/config/vfn-identity.yaml
    discovery_method:
      onchain    # ← VFN 使用链上发现

consensus:
  enable_pipeline: true
  # ... 其他共识配置
```

### 2.2 VFN 身份密钥

**文件位置：** `deploy_utils/vfn/config/vfn-identity.yaml`

```yaml
account_address: d07f2afb452b481500825ec466d810c0ffd80d928c55175b9eb936628abeb759
account_private_key: 00629218f9c37a5699893ba22ba7c23a3a56504e1cf5a19169a5f59b47bd5930
consensus_private_key: 6e3b7c496fbbd114d3ce2d2403a6972898397f2b5cac2231481f629673fc9016
network_private_key: c8392be2d52b5a293e0db852ac75bfbd5de50fb39cc280d404da843649548a62
```

**密钥用途：**

| 密钥 | 类型 | 用途 |
|-----|------|-----|
| `account_address` | AccountAddress (32 bytes) | VFN 的 peer_id 标识 |
| `account_private_key` | Ed25519 (32 bytes) | 链上交易签名 |
| `consensus_private_key` | BLS12-381 (32 bytes) | 共识投票（如果成为验证者） |
| `network_private_key` | x25519 (32 bytes) | P2P 网络加密（Noise 协议） |

## 3. 连接建立流程

### 3.1 完整连接流程图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      VFN 与 Validator 连接建立流程                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  VFN 节点                                              Validator 节点        │
│     │                                                       │               │
│     │  1. 启动并加载配置                                      │               │
│     │     ├── 加载 identity.yaml                             │               │
│     │     └── 解析 discovery_method: onchain                 │               │
│     │                                                       │               │
│     │  2. 链上发现 (On-Chain Discovery)                      │               │
│     │     ├── 读取链上 ValidatorConfig 资源                   │               │
│     │     └── 获取所有验证者的网络地址和 peer_id              │               │
│     │                                                       │               │
│     │  3. 建立 TCP 连接                                      │               │
│     │─────────────────TCP Connect─────────────────────────►│               │
│     │                                                       │               │
│     │  4. Noise_IK 握手                                     │               │
│     │─────────────────e, es, s, ss────────────────────────►│               │
│     │◄────────────────e, ee, se────────────────────────────│               │
│     │                                                       │               │
│     │  5. 协议协商                                           │               │
│     │     ├── 交换支持的协议列表                              │               │
│     │     └── 协商 Consensus/Mempool 协议版本                 │               │
│     │                                                       │               │
│     │  6. 连接就绪                                           │               │
│     │     └── 加入 PeersAndMetadata                          │               │
│     │                                                       │               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Peer 初始化

**代码位置：** `crates/api/src/bootstrap.rs:263-270`

```rust
pub fn init_peers_and_metadata(
    node_config: &NodeConfig,
    _consensus_db: &Arc<ConsensusDB>,
) -> Arc<PeersAndMetadata> {
    let network_ids = extract_network_ids(node_config);
    PeersAndMetadata::new(&network_ids)
}
```

`PeersAndMetadata` 负责跟踪所有网络的 peer 连接状态和元数据。

### 3.3 网络协议注册

**代码位置：** `crates/api/src/consensus_api.rs:176-220`

```rust
// 对每个网络 (Validator, VFN):
for network_config in network_configs.into_iter() {
    let runtime = create_network_runtime(&network_config);
    let mut network_builder = NetworkBuilder::create(...);

    // 仅 Validator 网络注册 JWK 和 DKG
    if network_id.is_validator_network() {
        jwk_consensus_network_handle = Some(register_client_and_service_with_network(...));
        dkg_network_handle = Some(register_client_and_service_with_network::<DKGMessage>(...));
    }

    // 所有网络都注册 Consensus 协议
    let network_handle = register_client_and_service_with_network(
        &mut network_builder,
        network_id,
        &network_config,
        consensus_network_configuration(&node_config),  // Direct Send + RPC
        true,
    );
    consensus_network_handles.push(network_handle);

    // 所有网络都注册 Mempool 协议
    let mempool_network_handle = register_client_and_service_with_network(
        &mut network_builder,
        network_id,
        &network_config,
        mempool_network_configuration(&node_config),  // Direct Send only
        true,
    );
    mempool_network_handles.push(mempool_network_handle);

    network_builder.build(runtime.handle().clone());
    network_builder.start();
}
```

### 3.4 协议注册矩阵

| 协议 | Validator 网络 | VFN 网络 | 通信模式 |
|-----|---------------|----------|---------|
| Consensus | ✓ | ✓ | Direct Send + RPC |
| Mempool | ✓ | ✓ | Direct Send |
| JWK Consensus | ✓ | ✗ | Direct Send + RPC |
| DKG | ✓ | ✗ | Direct Send + RPC |

## 4. 区块同步机制

### 4.1 BlockRetriever 结构

**代码位置：** `aptos-core/consensus/src/block_storage/sync_manager.rs:873-900`

```rust
pub struct BlockRetriever {
    network_id: NetworkId,                    // Validator 或 VFN
    network: Arc<NetworkSender>,              // 网络发送接口
    preferred_peer: Author,                   // 首选 peer（通常是提议者）
    available_peers: Vec<AccountAddress>,     // 可用 peer 列表
    max_blocks_to_request: u64,              // 每次请求的最大区块数
    pending_blocks: Arc<Mutex<PendingBlocks>>, // 待处理区块追踪
}
```

### 4.2 Validator vs VFN 的 Peer 获取差异

**代码位置：** `aptos-core/consensus/src/round_manager.rs:321-359`

```rust
fn create_block_retriever(&self, author: Author) -> BlockRetriever {
    let (network_id, available_peers) = if self.is_validator() {
        // ━━━━━━━━━━ Validator 模式 ━━━━━━━━━━
        // 从链上验证者集合获取 peer 列表
        (
            NetworkId::Validator,
            self.epoch_state
                .verifier
                .get_ordered_account_addresses_iter()
                .filter(|addr| *addr != author)  // 排除提议者
                .collect(),
        )
    } else {
        // ━━━━━━━━━━ VFN/FullNode 模式 ━━━━━━━━━━
        // 从网络层动态获取已连接的 VFN peer
        let available_peers = self
            .network
            .consensus_network_client
            .network_client
            .get_available_peers()
            .map(|peers| {
                peers
                    .iter()
                    .filter(|peer| peer.network_id() == NetworkId::Vfn)  // 只选 VFN 网络
                    .map(|peer| peer.peer_id())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_else(|e| {
                error!("Failed to get available peers: {:?}", e);
                vec![]
            });
        (NetworkId::Vfn, available_peers)
    };

    BlockRetriever::new(
        network_id,
        self.network.clone(),
        author,
        available_peers,
        self.local_config.max_blocks_per_sending_request(...),
        self.block_store.pending_blocks(),
    )
}
```

**关键差异：**

| 模式 | Peer 来源 | 网络 ID |
|-----|----------|---------|
| Validator | 链上 epoch 验证者集合 (`epoch_state.verifier`) | `NetworkId::Validator` |
| VFN | 动态发现的已连接 VFN peer (`get_available_peers()`) | `NetworkId::Vfn` |

### 4.3 区块同步流程图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         VFN 区块同步完整流程                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. 触发同步 (收到 SyncInfo 消息)                                            │
│     │                                                                       │
│     ▼                                                                       │
│  2. 检查是否需要同步                                                         │
│     │  need_sync_for_ledger_info():                                         │
│     │    - local_round + 30 < remote_round ?                                │
│     │    - 或 local_round + 2*vote_back_pressure_limit < remote_round ?     │
│     │                                                                       │
│     ▼                                                                       │
│  3. 创建 BlockRetriever                                                     │
│     │  create_block_retriever(proposer):                                    │
│     │    - VFN: 使用 NetworkId::Vfn + get_available_peers()                 │
│     │    - Validator: 使用 NetworkId::Validator + epoch_verifier            │
│     │                                                                       │
│     ▼                                                                       │
│  4. 执行同步 (add_certs)                                                    │
│     │                                                                       │
│     ├──► 4.1 sync_to_highest_commit_cert()                                  │
│     │        同步到最高提交证书                                              │
│     │                                                                       │
│     ├──► 4.2 sync_to_highest_quorum_cert()                                  │
│     │        同步到最高 QC (可能触发 fast_forward_sync)                      │
│     │                                                                       │
│     ├──► 4.3 insert_quorum_cert()                                           │
│     │        插入 QC 及其依赖区块                                            │
│     │                                                                       │
│     └──► 4.4 insert_ordered_cert() (如果启用 order_vote)                    │
│              插入排序证书                                                    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 4.4 区块检索请求

**代码位置：** `aptos-core/consensus/consensus-types/src/block_retrieval.rs`

```rust
// 重试配置
pub const NUM_RETRIES: usize = 5;           // 最多重试 5 次
pub const NUM_PEERS_PER_RETRY: usize = 1;   // 每次重试询问 1 个 peer
pub const RETRY_INTERVAL_MSEC: u64 = 500;   // 重试间隔 500ms
pub const RPC_TIMEOUT_MSEC: u64 = 5000;     // RPC 超时 5000ms

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct BlockRetrievalRequest {
    block_id: HashValue,              // 起始区块 ID
    num_blocks: u64,                  // 请求的区块数量
    target_block_id: Option<HashValue>, // 目标区块 ID（可选）
    epoch: Option<u64>,               // 指定 epoch（可选）
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub enum BlockRetrievalStatus {
    Succeeded,              // 成功获取所有区块
    IdNotFound,            // 区块未找到
    NotEnoughBlocks,       // 部分获取
    SucceededWithTarget,   // 找到目标区块
    QuorumCertNotFound,    // QC 未找到
}
```

### 4.5 区块检索重试算法

**代码位置：** `aptos-core/consensus/src/block_storage/sync_manager.rs:902-990`

```rust
async fn retrieve_block_for_id_chunk(
    &mut self,
    block_id: HashValue,
    target_block_id: HashValue,
    retrieve_batch_size: u64,
    mut peers: Vec<AccountAddress>,
    epoch: Option<u64>,
) -> anyhow::Result<BlockRetrievalResponse> {
    let mut failed_attempt = 0_u32;
    let mut cur_retry = 0;

    loop {
        tokio::select! {
            // 定时器触发，发送下一批请求
            _ = interval.tick() => {
                let next_peers = if cur_retry < NUM_RETRIES {
                    let first_attempt = cur_retry == 0;
                    cur_retry += 1;
                    // 首次尝试 preferred_peer，之后随机选择
                    self.pick_peers(
                        first_attempt,
                        &mut peers,
                        if first_attempt { 1 } else { NUM_PEERS_PER_RETRY }
                    )
                } else {
                    Vec::new()
                };

                if next_peers.is_empty() && futures.is_empty() {
                    bail!("Couldn't fetch block")
                }

                // 向选中的 peer 发送请求
                for peer in next_peers {
                    let future = self.network.request_block(
                        request.clone(),
                        PeerNetworkId::new(self.network_id, peer),
                        rpc_timeout,  // 5000ms
                    );
                    futures.push(future);
                }
            }
            // 处理响应
            Some((peer, response)) = futures.next() => {
                match response {
                    Ok(result) => return Ok(result),
                    e => {
                        warn!("Failed to fetch block from {}: {:?}", peer, e);
                        failed_attempt += 1;
                    },
                }
            },
        }
    }
}
```

**重试策略：**

```
┌─────────────────────────────────────────────────────────────────┐
│                    区块检索重试策略                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   第 1 次尝试 ──► preferred_peer (提议者)                        │
│        │                                                        │
│        ▼ (失败，等待 500ms)                                      │
│   第 2 次尝试 ──► 随机选择 1 个 peer                             │
│        │                                                        │
│        ▼ (失败，等待 500ms)                                      │
│   第 3 次尝试 ──► 随机选择 1 个 peer                             │
│        │                                                        │
│        ▼ (失败，等待 500ms)                                      │
│   第 4 次尝试 ──► 随机选择 1 个 peer                             │
│        │                                                        │
│        ▼ (失败，等待 500ms)                                      │
│   第 5 次尝试 ──► 随机选择 1 个 peer                             │
│        │                                                        │
│        ▼ (失败)                                                 │
│   返回错误: "Couldn't fetch block"                              │
│                                                                 │
│   每次 RPC 超时: 5000ms                                         │
│   重试间隔: 500ms                                               │
│   最大总耗时: ~27.5 秒                                          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.6 Fast Forward Sync（快速追赶同步）

当本地状态落后较多时（>30 轮或 2x vote_back_pressure_limit），触发快速追赶同步。

**代码位置：** `aptos-core/consensus/src/block_storage/sync_manager.rs:418-512`

```rust
pub async fn fast_forward_sync<'a>(
    highest_quorum_cert: &'a QuorumCert,
    highest_commit_cert: &'a WrappedLedgerInfo,
    retriever: &'a mut BlockRetriever,
    storage: Arc<dyn PersistentLivenessStorage>,
    payload_manager: Arc<dyn TPayloadManager>,
    order_vote_enabled: bool,
    is_validator: bool,
) -> anyhow::Result<(Vec<(Block, Option<Vec<u8>>)>, Vec<QuorumCert>)> {
    info!(
        "Start block sync to commit cert: {}, quorum cert: {}",
        highest_commit_cert, highest_quorum_cert,
    );

    // 计算需要获取的区块数量
    let num_blocks = highest_quorum_cert.certified_block().round() -
        highest_commit_cert.ledger_info().ledger_info().round() + 1;

    // 批量获取区块
    let (mut blocks, _, ledger_infos) = retriever
        .retrieve_blocks_in_range(
            highest_quorum_cert.certified_block().id(),
            num_blocks,
            highest_commit_cert.commit_info().id(),
            if is_validator {
                // Validator: 使用 QC 签名者作为 peer
                highest_quorum_cert.ledger_info().get_voters(&retriever.available_peers)
            } else {
                // VFN: 使用所有可用 peer
                retriever.available_peers.clone()
            },
            payload_manager.clone(),
        )
        .await?;

    // 构建 QC 链
    let mut quorum_certs = vec![highest_quorum_cert.clone()];
    quorum_certs.extend(
        blocks.iter().take(blocks.len() - 1).map(|(block, _)| block.quorum_cert().clone()),
    );

    // 保存到存储
    storage.save_tree(
        blocks.iter().map(|(block, _)| block.clone()).collect(),
        quorum_certs.clone(),
        block_numbers,
    )?;

    // 反转顺序（从旧到新）
    blocks.reverse();
    quorum_certs.reverse();

    Ok((blocks, quorum_certs))
}
```

### 4.7 同步触发条件

**代码位置：** `aptos-core/consensus/src/block_storage/sync_manager.rs:86-99`

```rust
impl BlockStore {
    /// 检查是否需要同步
    pub fn need_sync_for_ledger_info(&self, li: &LedgerInfoWithSignatures) -> bool {
        // 条件 1: 本地已排序的轮次 < 远程提交轮次，且区块不存在
        (self.ordered_root().round() < li.commit_info().round() &&
            !self.block_exists(li.commit_info().id())) ||
        // 条件 2: 本地提交轮次 + 阈值 < 远程提交轮次
        self.commit_root().round() + 30.max(2 * self.vote_back_pressure_limit) <
            li.commit_info().round()
    }

    pub fn need_sync_to_highest_quorum_cert(&self, hqc: &QuorumCert) -> bool {
        self.ordered_root().round() < hqc.certified_block().round() &&
            !self.block_exists(hqc.certified_block().id())
    }
}
```

**同步触发条件总结：**

| 条件 | 描述 |
|-----|------|
| 区块缺失 | `ordered_root.round < remote_round` 且本地无该区块 |
| 落后过多 | `commit_root.round + max(30, 2*back_pressure_limit) < remote_round` |

## 5. Peer 发现机制

### 5.1 VFN 链上发现 (On-Chain Discovery)

VFN 配置使用 `discovery_method: onchain`，这意味着：

1. **查询链上状态**：VFN 读取链上 `0x1::stake::ValidatorConfig` 资源
2. **提取验证者信息**：获取所有验证者的网络地址和 peer_id
3. **动态更新**：随着 epoch 变化，验证者列表自动更新
4. **无静态配置**：不需要手动配置 seeds

### 5.2 Validator 文件发现

Validator 配置使用文件发现：

```yaml
validator_network:
  discovery_method:
    file:
      path: "/tmp/gravity_node/discovery"
      interval_secs: 3600
```

配合 `network_config.json` 提供静态 peer 列表。

### 5.3 发现方式对比

| 节点类型 | 发现方式 | 配置 | 更新频率 |
|---------|---------|------|---------|
| Validator | 文件发现 | `discovery_method: file` | 每 3600 秒 |
| VFN | 链上发现 | `discovery_method: onchain` | 实时（epoch 变化时） |

## 6. 完整同步流程示例

### 6.1 VFN 同步场景

假设：
- VFN 当前 commit_root.round = 100
- 收到 SyncInfo，其中 highest_commit_cert.round = 200

```
VFN 节点                                              Validator 节点
    │                                                       │
    │  1. 收到 SyncInfo (commit_round=200)                   │
    │◄──────────────────────────────────────────────────────│
    │                                                       │
    │  2. 检查: need_sync_for_ledger_info() = true          │
    │     (100 + 30 < 200)                                  │
    │                                                       │
    │  3. 创建 BlockRetriever                               │
    │     network_id = NetworkId::Vfn                       │
    │     available_peers = get_available_peers()           │
    │                                                       │
    │  4. 发送 BlockRetrievalRequest                        │
    │     block_id = highest_qc.certified_block_id          │
    │     num_blocks = 101                                  │
    │─────────────────RPC Request──────────────────────────►│
    │                                                       │
    │  5. 接收 BlockRetrievalResponse                       │
    │◄────────────────blocks + QCs─────────────────────────│
    │                                                       │
    │  6. 验证并保存区块                                      │
    │     storage.save_tree(blocks, qcs)                    │
    │                                                       │
    │  7. 重建内存状态                                        │
    │     self.rebuild(root, blocks, quorum_certs)          │
    │                                                       │
    │  8. 同步完成，继续正常共识                              │
    │                                                       │
```

## 7. 关键代码文件索引

| 功能 | 文件路径 |
|------|---------|
| 网络配置提取 | `crates/api/src/network.rs` |
| 共识引擎初始化 | `crates/api/src/consensus_api.rs` |
| Peer 初始化 | `crates/api/src/bootstrap.rs` |
| BlockRetriever 实现 | `aptos-core/consensus/src/block_storage/sync_manager.rs` |
| 区块检索协议 | `aptos-core/consensus/consensus-types/src/block_retrieval.rs` |
| Peer 选择逻辑 | `aptos-core/consensus/src/round_manager.rs` |
| VFN 配置示例 | `deploy_utils/vfn/config/validator_full_node.yaml` |
| VFN 身份配置 | `deploy_utils/vfn/config/vfn-identity.yaml` |

## 8. 总结

### 8.1 VFN 同步设计特点

| 特性 | 说明 |
|-----|------|
| **动态 Peer 发现** | 使用链上发现，无需静态配置 |
| **角色切换** | VFN 网络临时使用 FullNode 角色 |
| **重试机制** | 5 次重试，首选提议者，随后随机选择 |
| **快速追赶** | 落后 30+ 轮时触发 fast_forward_sync |
| **批量获取** | 可配置的 max_blocks_per_sending_request |

### 8.2 与 Aptos 的差异

| 方面 | Gravity | Aptos |
|-----|---------|-------|
| VFN 用途 | 非当前 epoch 验证者区块同步 | 公共访问入口 |
| PFN 层 | 无 | 有 |
| Peer 发现 | 文件 (Validator) + 链上 (VFN) | 链上 (全部) |
| 密钥存储 | 文件 | Vault |

### 8.3 同步性能参数

| 参数 | 默认值 | 说明 |
|-----|-------|------|
| `NUM_RETRIES` | 5 | 最大重试次数 |
| `NUM_PEERS_PER_RETRY` | 1 | 每次重试询问的 peer 数 |
| `RETRY_INTERVAL_MSEC` | 500 | 重试间隔 (ms) |
| `RPC_TIMEOUT_MSEC` | 5000 | RPC 超时 (ms) |
| 快速追赶阈值 | 30 轮 | 触发 fast_forward_sync |
