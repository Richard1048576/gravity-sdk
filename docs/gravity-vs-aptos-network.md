# Gravity vs Aptos 网络实现对比

本文档详细对比 Gravity SDK 和 Aptos 在网络连接层面的实现差异。

## 1. 架构概览

### Aptos 网络架构（三层结构）

```
┌────────────────────────────────────────────────────────────────────────────┐
│                         Aptos 网络架构                                      │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│   Validator ◄──────► Validator    (validator_network, 双向认证)             │
│       │                                                                    │
│       ▼ (vfn network, 单向认证)                                             │
│      VFN ◄──────────► VFN         (全节点间同步)                            │
│       │                                                                    │
│       ▼ (public network)                                                   │
│      PFN ◄──────────► PFN         (公共网络，对外服务)                       │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

### Gravity 网络架构（两层结构）

```
┌────────────────────────────────────────────────────────────────────────────┐
│                         Gravity 网络架构                                    │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│   Validator ◄──────► Validator    (validator_network, 双向认证)             │
│       │                                                                    │
│       ▼ (vfn network)                                                      │
│      VFN (用于非当前 epoch 验证者的区块同步)                                  │
│                                                                            │
│   注意: 没有 PFN (Public Full Node) 层                                      │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

## 2. 关键差异对比表

| 方面 | Aptos | Gravity |
|-----|-------|---------|
| **网络层级** | 3层 (Validator, VFN, PFN) | 2层 (Validator, VFN) |
| **密钥存储** | Vault 安全存储 (`from_storage`) | 文件存储 (`from_file`) |
| **Peer 发现** | 链上 (`onchain`) + 静态 seeds | 文件 (`file`) + 链上 |
| **VFN 用途** | 公共访问入口 | 非当前 epoch 验证者的区块同步 |
| **配置格式** | YAML + Vault 后端 | YAML + JSON 文件 |
| **网络底层** | `aptos-network` | `gaptos` (自定义 fork) |

## 3. Identity 配置对比

### 3.1 Aptos 配置方式（Vault 存储）

**validator.yaml:**
```yaml
validator_network:
  discovery_method: "onchain"
  listen_address: "/ip4/0.0.0.0/tcp/6180"
  identity:
    type: "from_storage"
    key_name: "validator_network"
    peer_id_name: "owner_account"
    backend:
      type: "vault"
      server: "https://127.0.0.1:8200"
      ca_certificate: "/full/path/to/certificate"
      token:
        from_disk: "/full/path/to/token"
  mutual_authentication: true

full_node_networks:
  - listen_address: "/ip4/0.0.0.0/tcp/6181"
    identity:
      type: "from_storage"
      key_name: "fullnode_network"      # 不同的网络密钥
      peer_id_name: "owner_account"     # 相同的 peer_id
      backend:
        type: "vault"
        # ...
    network_id:
      private: "vfn"
```

**密钥分离设计：**
- `validator_network` 和 `fullnode_network` 使用**不同的 x25519 密钥**
- 但共享相同的 `owner_account` 作为 peer_id

### 3.2 Gravity 配置方式（文件存储）

**validator.yaml:**
```yaml
base:
  role: "validator"
  data_dir: "/tmp/gravity_node/data"
  waypoint:
    from_file: "/tmp/gravity_node/config/waypoint.txt"

validator_network:
  network_id: validator
  listen_address: "/ip4/127.0.0.1/tcp/2024"
  discovery_method:
    file:
      path: "/tmp/gravity_node/discovery"
      interval_secs: 3600
  mutual_authentication: true
  identity:
    type: "from_file"
    path: /tmp/gravity_node/config/identity.yaml

consensus:
  enable_pipeline: true
  safety_rules:
    backend:
      type: "on_disk_storage"
      path: /tmp/gravity_node/data/secure_storage.json
```

**identity.yaml:**
```yaml
account_address: 2d86b40a1d692c0749a0a0426e2021ee24e2430da0f5bb9c2ae6c586bf3e0a0f
account_private_key: 49e1215cc2c1963a39c72dd41c3639ab0615ad50dbdd48548e358fb516ba259b
consensus_private_key: "0x08f4f119547f55b86f8700963323585afc91e346f55d7a4bcfd928f3386834b8"
network_private_key: 200912a088598014b88cd1fb91dbdf2df18b352184a727c076f07cfd145fa267
```

**密钥类型说明：**

| 密钥 | 类型 | 长度 | 用途 |
|-----|------|------|-----|
| `account_address` | AccountAddress | 32 bytes | 验证者身份标识（peer_id） |
| `account_private_key` | Ed25519 | 32 bytes | 链上交易签名 |
| `consensus_private_key` | BLS12-381 | 32 bytes | 共识投票签名 |
| `network_private_key` | x25519 | 32 bytes | P2P 网络加密通信 |

## 4. VFN 特殊处理逻辑

### 4.1 Gravity 的 VFN 角色切换

**代码位置：** `crates/api/src/consensus_api.rs:161-169`

```rust
let mut network_builder = NetworkBuilder::create(
    chain_id,
    if network_id.is_vfn_network() {
        // FIXME(nekomoto): This is a temporary solution to support block sync for
        // validator node which is not the current epoch validator.
        RoleType::FullNode  // ← VFN 网络临时使用 FullNode 角色
    } else {
        node_config.base.role  // ← Validator 网络使用配置的角色
    },
    &network_config,
    gaptos::aptos_time_service::TimeService::real(),
    Some(&mut event_subscription_service),
    peers_and_metadata.clone(),
);
```

**设计意图：**
- 当验证者不在当前 epoch 的验证者集合中时
- 需要通过 VFN 网络同步区块
- 此时角色临时切换为 FullNode 以获取区块同步能力

### 4.2 VFN 配置示例

**validator_full_node.yaml:**
```yaml
base:
  role: "full_node"
  data_dir: "/tmp/vfn/data"

full_node_networks:
  - network_id:
      private: "vfn"
    listen_address: "/ip4/127.0.0.1/tcp/2044"
    identity:
      type: "from_file"
      path: /tmp/vfn/config/vfn-identity.yaml
    discovery_method:
      onchain    # VFN 使用链上发现
```

## 5. Peer 发现机制对比

### 5.1 Aptos 发现机制

```yaml
# Validator 网络：链上发现
validator_network:
  discovery_method: "onchain"

# VFN 网络：静态 seeds 配置
full_node_networks:
  - network_id:
      private: "vfn"
    seeds:
      00000000000000000000000000000000d58bc7bb154b38039bc9096ce04e1237:
        addresses:
        - "/ip4/127.0.0.1/tcp/6181/noise-ik/f0274c2774519281.../handshake/0"
        role: "Validator"
```

### 5.2 Gravity 发现机制

**Validator 使用文件发现：**
```yaml
validator_network:
  discovery_method:
    file:
      path: "/tmp/gravity_node/discovery"
      interval_secs: 3600
```

**network_config.json（peer 信息）：**
```json
{
    "/ip4/127.0.0.1/tcp/2024": {
        "consensus_public_key": "851d41932d866f5fabed6673898e15473e6a0adcf5033d2c93816c6b115c85ad3451e0bac61d570d5ed9f23e1e7f77c4",
        "account_address": "2d86b40a1d692c0749a0a0426e2021ee24e2430da0f5bb9c2ae6c586bf3e0a0f",
        "network_public_key": "2d86b40a1d692c0749a0a0426e2021ee24e2430da0f5bb9c2ae6c586bf3e0a0f",
        "trusted_peers_map": [],
        "public_ip_address": "/ip4/127.0.0.1/tcp/2024",
        "voting_power": 1
    }
}
```

**发现方式对比：**

| 网络类型 | Aptos | Gravity |
|---------|-------|---------|
| Validator | 链上发现 (`onchain`) | 文件发现 (`file`) |
| VFN | 静态 seeds | 链上发现 (`onchain`) |
| PFN | 链上发现 | 不适用（无 PFN） |

## 6. 网络协议注册

### 6.1 协议类型

**代码位置：** `crates/api/src/network.rs`

```rust
// 1. Consensus 协议 (Direct Send + RPC)
pub fn consensus_network_configuration(node_config: &NodeConfig) -> NetworkApplicationConfig {
    let direct_send_protocols: Vec<ProtocolId> =
        aptos_consensus::network_interface::DIRECT_SEND.into();
    let rpc_protocols: Vec<ProtocolId> =
        aptos_consensus::network_interface::RPC.into();
    // ...
}

// 2. Mempool 协议 (仅 Direct Send，无 RPC)
pub fn mempool_network_configuration(node_config: &NodeConfig) -> NetworkApplicationConfig {
    let direct_send_protocols = vec![ProtocolId::MempoolDirectSend];
    let rpc_protocols = vec![];  // Mempool 不使用 RPC
    // ...
}

// 3. JWK Consensus 协议 (仅 Validator 网络)
pub fn jwk_consensus_network_configuration(node_config: &NodeConfig) -> NetworkApplicationConfig

// 4. DKG 协议 (仅 Validator 网络)
pub fn dkg_network_configuration(node_config: &NodeConfig) -> NetworkApplicationConfig
```

### 6.2 协议注册矩阵

| 协议 | Validator 网络 | VFN 网络 | 通信模式 |
|-----|---------------|----------|---------|
| Consensus | ✓ | ✓ | Direct Send + RPC |
| Mempool | ✓ | ✓ | Direct Send |
| JWK Consensus | ✓ | ✗ | Direct Send + RPC |
| DKG | ✓ | ✗ | Direct Send + RPC |

## 7. 网络初始化流程

### 7.1 Gravity 初始化流程图

```
ConsensusEngine::init()
    │
    ├── 1. aptos_crash_handler::setup_panic_handler()
    │
    ├── 2. ConsensusDB::new()                    // 初始化共识数据库
    │
    ├── 3. init_peers_and_metadata()             // 初始化 peer 元数据
    │       └── PeersAndMetadata::new(&network_ids)
    │
    ├── 4. extract_network_configs()             // 提取所有网络配置
    │
    ├── 5. 对每个网络配置:
    │       │
    │       ├── create_network_runtime()         // 创建独立 Tokio 运行时
    │       │
    │       ├── NetworkBuilder::create()         // 创建网络构建器
    │       │       │
    │       │       └── VFN 特殊处理:
    │       │           if network_id.is_vfn_network() {
    │       │               RoleType::FullNode   // 使用 FullNode 角色
    │       │           }
    │       │
    │       ├── 注册 JWK Consensus (仅 Validator 网络)
    │       │
    │       ├── 注册 DKG (仅 Validator 网络)
    │       │
    │       ├── 注册 Consensus 协议
    │       │
    │       ├── 注册 Mempool 协议
    │       │
    │       ├── network_builder.build()
    │       │
    │       └── network_builder.start()
    │
    ├── 6. create_network_interfaces()           // 创建应用层网络接口
    │       ├── consensus_interfaces
    │       ├── mempool_interfaces
    │       ├── jwk_consensus_interfaces
    │       └── dkg_interfaces
    │
    ├── 7. init_mempool()                        // 启动 Mempool
    │
    ├── 8. create_dkg_runtime()                  // 创建 DKG 运行时
    │
    ├── 9. init_jwk_consensus()                  // 初始化 JWK 共识
    │
    ├── 10. init_block_buffer_manager()          // 初始化区块缓冲管理器
    │
    ├── 11. start_consensus()                    // 启动共识
    │
    └── 12. event_subscription_service.notify_initial_configs()
```

### 7.2 关键代码片段

**代码位置：** `crates/api/src/consensus_api.rs`

```rust
impl ConsensusEngine {
    pub async fn init(args: ConsensusEngineArgs, pool: Box<dyn TxPool>) -> Arc<Self> {
        // ... 省略初始化代码 ...

        let network_configs = extract_network_configs(&node_config);

        // Create each network and register the application handles
        let mut consensus_network_handles = vec![];
        let mut mempool_network_handles = vec![];

        for network_config in network_configs.into_iter() {
            let runtime = create_network_runtime(&network_config);
            let _enter = runtime.enter();
            let network_id = network_config.network_id;

            let mut network_builder = NetworkBuilder::create(
                chain_id,
                if network_id.is_vfn_network() {
                    RoleType::FullNode
                } else {
                    node_config.base.role
                },
                &network_config,
                // ...
            );

            // 仅 Validator 网络注册 JWK 和 DKG
            if network_id.is_validator_network() {
                jwk_consensus_network_handle = Some(register_client_and_service_with_network(...));
                dkg_network_handle = Some(register_client_and_service_with_network::<DKGMessage>(...));
            }

            // 所有网络都注册 Consensus 和 Mempool
            consensus_network_handles.push(register_client_and_service_with_network(...));
            mempool_network_handles.push(register_client_and_service_with_network(...));

            network_builder.build(runtime.handle().clone());
            network_builder.start();
            runtimes.push(runtime);
        }
        // ...
    }
}
```

## 8. 认证机制

### 8.1 双向认证强制检查

**代码位置：** `crates/api/src/network.rs:42-44`

```rust
pub fn extract_network_configs(node_config: &NodeConfig) -> Vec<NetworkConfig> {
    let mut network_configs: Vec<NetworkConfig> = node_config.full_node_networks.to_vec();
    if let Some(network_config) = node_config.validator_network.as_ref() {
        // Ensure that mutual authentication is enabled by default!
        if !network_config.mutual_authentication {
            panic!("Validator networks must always have mutual_authentication enabled!");
        }
        network_configs.push(network_config.clone());
    }
    network_configs
}
```

### 8.2 认证对比

| 网络类型 | Aptos | Gravity |
|---------|-------|---------|
| Validator | 双向认证（强制） | 双向认证（强制） |
| VFN | 单向认证 | 依配置而定 |
| PFN | 无认证 | 不适用 |

## 9. 代码目录结构

### 9.1 Gravity SDK 网络相关文件

```
gravity-sdk/
├── crates/
│   └── api/
│       └── src/
│           ├── network.rs              # 网络配置和接口创建
│           ├── consensus_api.rs        # 共识引擎初始化（含网络）
│           └── bootstrap.rs            # 启动引导和 peer 初始化
│
├── aptos-core/
│   └── consensus/
│       └── src/
│           └── network.rs              # 共识网络协议实现
│
├── template_config/
│   ├── validator.yaml                  # 验证者配置模板
│   ├── identity.yaml                   # 身份密钥模板
│   └── network_config.json             # Peer 发现配置
│
└── deploy_utils/
    └── vfn/
        └── config/
            └── validator_full_node.yaml  # VFN 配置模板
```

### 9.2 关键依赖

```toml
# Cargo.toml
[dependencies]
gaptos = { git = "https://github.com/Galxe/gravity-aptos.git", rev = "1df8aff" }
```

Gravity 使用 `gaptos`（Aptos 的自定义 fork）作为网络层基础。

## 10. 总结

### 10.1 Gravity 网络设计特点

| 特性 | 说明 |
|-----|------|
| **简化拓扑** | 移除 PFN 层，仅保留 Validator 和 VFN 两层网络 |
| **VFN 特殊用途** | 专用于 epoch 切换时的验证者区块同步，而非公共入口 |
| **文件配置** | 使用 `identity.yaml` + `network_config.json` 替代 Vault |
| **文件发现** | 验证者间使用文件发现，VFN 使用链上发现 |
| **GCEI 集成** | 通过 `block_buffer_manager` 与执行层（Reth）集成 |
| **Pipeline 共识** | 启用 `enable_pipeline: true` 提升吞吐量 |

### 10.2 与 Aptos 的主要差异

1. **网络层级**：Gravity 采用两层架构，Aptos 采用三层架构
2. **VFN 定位**：Gravity 的 VFN 仅用于验证者同步，Aptos 的 VFN 是公共访问入口
3. **密钥管理**：Gravity 使用文件存储，Aptos 推荐 Vault 安全存储
4. **Peer 发现**：Gravity 验证者使用文件发现，Aptos 使用链上发现
5. **执行层**：Gravity 集成 Reth，Aptos 使用原生 Move VM

### 10.3 代码参考

| 组件 | Gravity 路径 | Aptos 路径 |
|------|-------------|-----------|
| 网络配置 | `crates/api/src/network.rs` | `config/src/config/network_config.rs` |
| 身份配置 | `template_config/identity.yaml` | `config/src/config/identity_config.rs` |
| 共识初始化 | `crates/api/src/consensus_api.rs` | `aptos-node/src/lib.rs` |
| Peer 发现 | `template_config/network_config.json` | 链上 `ValidatorConfig` |
